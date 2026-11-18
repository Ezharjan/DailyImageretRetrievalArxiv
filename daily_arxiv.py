#!/usr/bin/env python3
"""Image Retrieval arXiv Daily.

Fetches the newest arXiv papers for every research topic defined in
``config.yaml``, merges them into a JSON paper database, links the authors' code
repositories and regenerates the data files used by the GitHub Pages site.

Usage
-----
    python daily_arxiv.py                        # daily update (fetch new papers)
    python daily_arxiv.py --max_results 300      # one-off deeper backfill
    python daily_arxiv.py --dry_run --topic ID   # list what one topic matches, write nothing
    python daily_arxiv.py --update_paper_links   # re-check code links of recent papers
    python daily_arxiv.py --rebuild              # regenerate site files without network

Outputs (paths configurable in ``config.yaml``)
-----------------------------------------------
    docs/data/papers.json            paper database read by the website
    docs/data/abstracts/YYMM.json    abstracts, split by arXiv month
    docs/_data/topics.json           topic definitions (About page)
    docs/index.md                    papers page front matter
    docs/feed.xml                    Atom feed of recently added papers

Only ``requests`` and ``PyYAML`` are required. The arXiv Atom API is parsed with
the standard library, so the tracker does not break when third-party arXiv
client libraries change their interfaces.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import requests
import yaml

__version__ = "2.0.0"

LOG = logging.getLogger("arxiv_daily")

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_ABS_URL = "https://arxiv.org/abs/{}"
ARXIV_PDF_URL = "https://arxiv.org/pdf/{}"
HF_PAPER_API_URL = "https://huggingface.co/api/papers/{}"

#: Version of the papers.json layout. Bump it when the format changes.
SCHEMA_VERSION = 1

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

#: HTTP statuses worth retrying (rate limiting and transient server errors).
_RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})


class ConfigError(Exception):
    """The configuration file is missing or invalid."""


class StoreError(Exception):
    """The paper database exists but cannot be read safely."""


class ArxivError(Exception):
    """An arXiv API request failed."""


class ArxivQueryError(ArxivError):
    """arXiv rejected a query (malformed search expression); not retried."""


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TEX_ESCAPE_RE = re.compile(r"\\([&%#_$])")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_CATEGORY_RE = re.compile(r"^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z]+(?:-[A-Za-z]+)*)?$")
_TOPIC_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ARXIV_ID_RE = re.compile(r"arxiv\.org/abs/(?P<id>.+?)(?:v(?P<version>\d+))?$")
_NEW_STYLE_ID_RE = re.compile(r"^(?P<month>\d{4})\.\d{4,5}$")


def normalize_space(value: Optional[str]) -> str:
    """Collapse all whitespace to single spaces and drop control characters."""
    if not value:
        return ""
    return _CTRL_RE.sub("", _WS_RE.sub(" ", value)).strip()


def clean_title(value: Optional[str]) -> str:
    """Normalize a title and unescape common TeX punctuation (``\\&`` -> ``&``)."""
    return _TEX_ESCAPE_RE.sub(r"\1", normalize_space(value))


def slugify(value: str) -> str:
    """Return a lowercase ASCII identifier such as ``composed-image-retrieval``."""
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_value.lower()).strip("-")


def iso_date(value: Optional[str]) -> str:
    """Return the ``YYYY-MM-DD`` part of an ISO-8601 timestamp, or ``""``."""
    text = (value or "").strip()
    if not _DATE_RE.match(text):
        return ""
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def format_timestamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def shard_key(paper_id: str) -> str:
    """Abstract file for a paper: the YYMM prefix of new-style arXiv IDs."""
    match = _NEW_STYLE_ID_RE.match(paper_id)
    return match.group("month") if match else "legacy"


# ---------------------------------------------------------------------------
# Code repository links
# ---------------------------------------------------------------------------

_REPO_URL_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?"
    r"(?P<host>github\.com|gitlab\.com|codeberg\.org|bitbucket\.org)/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_])?)/"
    r"(?P<repo>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
_ANONYMOUS_REPO_RE = re.compile(
    r"(?<![\w.-])(?:https?://)?anonymous\.4open\.science/r/(?P<repo>[A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
#: First path segments that are GitHub/GitLab pages rather than accounts.
_RESERVED_OWNERS = frozenset({
    "about", "apps", "blog", "collections", "contact", "customer-stories", "enterprise",
    "explore", "features", "help", "issues", "join", "login", "marketplace", "notifications",
    "orgs", "pricing", "pulls", "readme", "search", "security", "settings", "site",
    "solutions", "sponsors", "topics", "trending", "users",
})


def _clean_repo_name(name: str) -> str:
    name = name.rstrip(".")
    if name.lower().endswith(".git"):
        name = name[:-4]
    return name.rstrip(".")


def extract_code_url(*texts: Optional[str]) -> str:
    """Return the first source-code repository URL found in ``texts``.

    Recognizes GitHub, GitLab, Codeberg and Bitbucket repositories (normalized to
    ``https://host/owner/repo``) and anonymous.4open.science review links.
    """
    for text in texts:
        if not text:
            continue
        for match in _REPO_URL_RE.finditer(text):
            owner = match.group("owner")
            repo = _clean_repo_name(match.group("repo"))
            if owner.lower() in _RESERVED_OWNERS or not repo or repo in {".", ".."}:
                continue
            return f"https://{match.group('host').lower()}/{owner}/{repo}"
        match = _ANONYMOUS_REPO_RE.search(text)
        if match:
            repo = _clean_repo_name(match.group("repo"))
            if repo:
                return f"https://anonymous.4open.science/r/{repo}"
    return ""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_RAW_FILTER_RE = re.compile(r"\b(?:ti|abs|au|co|jr|cat|rn|all):", re.IGNORECASE)
_BOOLEAN_RE = re.compile(r"\b(?:AND|OR|ANDNOT)\b")
#: A raw filter that is one field term, such as ``ti:"image search"`` or ``au:smith``.
_SINGLE_TERM_RE = re.compile(r'''^(?:ti|abs|au|co|jr|cat|rn|all):(?:"[^"()]*"|[^\s"()]+)$''', re.IGNORECASE)
_ALLOWED_FIELDS = ("ti", "abs", "co", "all")
#: Longest search expression accepted for one topic (keeps request URLs well
#: below the limits of web servers and proxies once percent-encoded).
_MAX_QUERY_LENGTH = 4000
#: ASCII hyphen plus the Unicode hyphens, dashes and minus sign (U+2010-2015, U+2212).
_DASHES = "".join(chr(code) for code in (0x2D, 0x2010, 0x2011, 0x2012, 0x2013, 0x2014, 0x2015, 0x2212))
#: Characters replaced by a space in plain phrases: query syntax and dashes.
_PHRASE_BREAK_RE = re.compile("[" + re.escape('"():' + chr(92) + _DASHES) + "]")


@dataclasses.dataclass(frozen=True)
class Topic:
    id: str
    name: str
    description: str
    filters: Tuple[str, ...]
    categories: Tuple[str, ...]
    query: str


@dataclasses.dataclass(frozen=True)
class Settings:
    root: Path
    title: str
    description: str
    site_url: str
    repository: str
    max_results: int
    categories: Tuple[str, ...]
    search_fields: Tuple[str, ...]
    page_size: int
    request_delay: float
    timeout: float
    max_retries: int
    code_from_abstract: bool
    code_huggingface: bool
    code_refresh_days: int
    code_request_delay: float
    code_time_limit: float
    data_dir: Path
    site_page: Path
    topics_data: Path
    feed_path: Path
    feed_size: int
    retention_days: int
    topics: Tuple[Topic, ...]

    @property
    def user_agent(self) -> str:
        return f"ImageRetrievalArxivDaily/{__version__} (+https://github.com/{self.repository})"

    @property
    def topic_ids(self) -> List[str]:
        return [topic.id for topic in self.topics]


def is_raw_filter(value: str) -> bool:
    """True when a filter is a hand-written arXiv expression (field prefix or boolean)."""
    return bool(_RAW_FILTER_RE.search(value) or _BOOLEAN_RE.search(value) or "(" in value)


def sanitize_phrase(value: str) -> str:
    """Prepare a plain phrase for a quoted arXiv query.

    Query syntax characters are removed and hyphens become spaces: the arXiv API
    mishandles hyphenated query terms (they can return no results), while its
    index splits words at hyphens, so ``"sketch based"`` finds both "sketch-based"
    and "sketch based".

    >>> sanitize_phrase('Sketch-Based "image" retrieval (SBIR)')
    'Sketch Based image retrieval SBIR'
    """
    return normalize_space(_PHRASE_BREAK_RE.sub(" ", value))


def _is_grouped(expression: str) -> bool:
    """True when one pair of parentheses encloses the whole expression."""
    if not (expression.startswith("(") and expression.endswith(")")):
        return False
    depth = 0
    for index, char in enumerate(expression):
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth == 0 and index < len(expression) - 1:
            return False
    return depth == 0


def build_query(filters: Iterable[str], fields: Sequence[str], categories: Sequence[str]) -> str:
    """Build an arXiv ``search_query`` that ORs every filter and ANDs the categories.

    >>> build_query(["cross-modal retrieval"], ["ti", "abs"], ["cs.CV"])
    '(ti:"cross modal retrieval" OR abs:"cross modal retrieval") AND (cat:cs.CV)'
    >>> build_query(['ti:"image search"', 'abs:hashing ANDNOT abs:"hash table"'], ["ti"], [])
    '(ti:"image search" OR (abs:hashing ANDNOT abs:"hash table"))'
    """
    clauses: List[str] = []
    for item in filters:
        item = item.strip()
        if is_raw_filter(item):
            # A single field term joins the other clauses as it is; a longer
            # expression is kept together by parentheses.
            single = _SINGLE_TERM_RE.match(item) or _is_grouped(item)
            clauses.append(item if single else f"({item})")
        else:
            phrase = sanitize_phrase(item)
            clauses.extend(f'{field}:"{phrase}"' for field in fields)
    query = " OR ".join(clauses)
    if len(clauses) != 1 or not _is_grouped(query):
        query = f"({query})"
    if categories:
        query += " AND (" + " OR ".join(f"cat:{category}" for category in categories) + ")"
    return query


def _section(raw: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{key}' must be a mapping")
    return value


def _number(section: Dict[str, Any], key: str, default: float, where: str, *, minimum: float,
            maximum: float, integer: bool) -> Any:
    value = section.get(key, default)
    label = f"{where}.{key}" if where else key
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (integer and not isinstance(value, int)):
        kind = "an integer" if integer else "a number"
        raise ConfigError(f"'{label}' must be {kind}, got {value!r}")
    if not minimum <= value <= maximum:
        raise ConfigError(f"'{label}' must be between {minimum} and {maximum}, got {value}")
    return int(value) if integer else float(value)


def _boolean(section: Dict[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{where}.{key}' must be true or false, got {value!r}")
    return value


def _text(section: Dict[str, Any], key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{where}.{key}' must be a non-empty string")
    return value.strip()


def _categories(value: Any, where: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"'{where}' must be a non-empty list of arXiv categories")
    result: List[str] = []
    for item in value:
        if not isinstance(item, str) or not _CATEGORY_RE.match(item.strip()):
            raise ConfigError(f"'{where}' contains an invalid arXiv category: {item!r}")
        if item.strip() not in result:
            result.append(item.strip())
    return tuple(result)


def _check_raw_filter(value: str, where: str) -> None:
    if value.count('"') % 2:
        raise ConfigError(f"{where}: unbalanced double quotes in {value!r}")
    depth = 0
    for char in value:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            break
    if depth != 0:
        raise ConfigError(f"{where}: unbalanced parentheses in {value!r}")


def _parse_topics(raw_topics: Any, categories: Tuple[str, ...], fields: Tuple[str, ...]) -> Tuple[Topic, ...]:
    if not isinstance(raw_topics, dict) or not raw_topics:
        raise ConfigError("'keywords' must map at least one topic name to its settings")
    topics: List[Topic] = []
    seen_ids: Set[str] = set()
    for name, spec in raw_topics.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"Topic names must be non-empty strings, got {name!r}")
        name = name.strip()
        where = f"keywords.{name}"
        if not isinstance(spec, dict):
            raise ConfigError(f"'{where}' must be a mapping with a 'filters' list")
        topic_id = spec.get("id", slugify(name))
        if not isinstance(topic_id, str) or not _TOPIC_ID_RE.match(topic_id):
            raise ConfigError(f"'{where}.id' must look like 'my-topic' (lowercase letters, digits, hyphens)")
        if topic_id in seen_ids:
            raise ConfigError(f"Duplicate topic id '{topic_id}' ({where})")
        seen_ids.add(topic_id)
        description = spec.get("description", "")
        if not isinstance(description, str):
            raise ConfigError(f"'{where}.description' must be a string")
        raw_filters = spec.get("filters")
        if not isinstance(raw_filters, list) or not raw_filters:
            raise ConfigError(f"'{where}.filters' must be a non-empty list of search phrases")
        filters: List[str] = []
        for item in raw_filters:
            if not isinstance(item, str) or not item.strip():
                raise ConfigError(f"'{where}.filters' contains an empty or non-text entry: {item!r}")
            item = normalize_space(item)
            if is_raw_filter(item):
                _check_raw_filter(item, where)
            elif not sanitize_phrase(item):
                raise ConfigError(f"'{where}.filters' contains an unusable phrase: {item!r}")
            if item not in filters:
                filters.append(item)
        topic_categories = (_categories(spec["categories"], f"{where}.categories")
                            if "categories" in spec else categories)
        query = build_query(filters, fields, topic_categories)
        if len(query) > _MAX_QUERY_LENGTH:
            raise ConfigError(f"'{where}' produces a {len(query)}-character arXiv query "
                              f"(limit {_MAX_QUERY_LENGTH}); split it into smaller topics")
        topics.append(Topic(topic_id, name, normalize_space(description), tuple(filters), topic_categories, query))
    return tuple(topics)


def load_settings(config_path: Path) -> Settings:
    """Load and validate ``config.yaml``."""
    path = Path(config_path).resolve()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"Configuration file not found: {path}") from None
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from None
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping")

    root = path.parent
    site = _section(raw, "site")
    arxiv_cfg = _section(raw, "arxiv")
    code = _section(raw, "code_links")
    output = _section(raw, "output")

    repository = _text(site, "repository", "owner/repository", "site")
    if not re.match(r"^[\w.-]+/[\w.-]+$", repository):
        raise ConfigError("'site.repository' must look like 'owner/repository'")
    site_url = _text(site, "url", "https://example.github.io/", "site")
    if not re.match(r"^https?://", site_url):
        raise ConfigError("'site.url' must start with http:// or https://")

    fields_value = arxiv_cfg.get("search_fields", ["ti", "abs"])
    if (not isinstance(fields_value, list) or not fields_value
            or any(field not in _ALLOWED_FIELDS for field in fields_value)):
        raise ConfigError(f"'arxiv.search_fields' must be a list drawn from {list(_ALLOWED_FIELDS)}")
    fields = tuple(dict.fromkeys(fields_value))
    categories = _categories(arxiv_cfg.get("categories", ["cs.CV"]), "arxiv.categories")

    def resolve(key: str, default: str) -> Path:
        return root / _text(output, key, default, "output")

    return Settings(
        root=root,
        title=_text(site, "title", "Image Retrieval arXiv Daily", "site"),
        description=_text(site, "description", "Daily-updated arXiv papers on image retrieval.", "site"),
        site_url=site_url.rstrip("/") + "/",
        repository=repository,
        max_results=_number(raw, "max_results", 100, "", minimum=1, maximum=1000, integer=True),
        categories=categories,
        search_fields=fields,
        page_size=_number(arxiv_cfg, "page_size", 100, "arxiv", minimum=1, maximum=2000, integer=True),
        request_delay=_number(arxiv_cfg, "request_delay", 3.0, "arxiv", minimum=3.0, maximum=60.0, integer=False),
        timeout=_number(arxiv_cfg, "timeout", 60, "arxiv", minimum=5, maximum=600, integer=False),
        max_retries=_number(arxiv_cfg, "max_retries", 4, "arxiv", minimum=0, maximum=10, integer=True),
        code_from_abstract=_boolean(code, "from_abstract", True, "code_links"),
        code_huggingface=_boolean(code, "huggingface", True, "code_links"),
        code_refresh_days=_number(code, "refresh_days", 90, "code_links", minimum=1, maximum=3650, integer=True),
        code_request_delay=_number(code, "request_delay", 0.7, "code_links", minimum=0.0, maximum=30.0, integer=False),
        code_time_limit=60.0 * _number(code, "time_limit_minutes", 30, "code_links", minimum=1, maximum=300,
                                       integer=False),
        data_dir=resolve("data_dir", "docs/data"),
        site_page=resolve("site_page", "docs/index.md"),
        topics_data=resolve("topics_data", "docs/_data/topics.json"),
        feed_path=resolve("feed", "docs/feed.xml"),
        feed_size=_number(output, "feed_size", 100, "output", minimum=1, maximum=1000, integer=True),
        retention_days=_number(output, "retention_days", 0, "output", minimum=0, maximum=36500, integer=True),
        topics=_parse_topics(raw.get("keywords"), categories, fields),
    )


# ---------------------------------------------------------------------------
# arXiv API client
# ---------------------------------------------------------------------------

def _retry_after_seconds(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return min(max(float(value), 1.0), 300.0)
    except ValueError:
        return None


def parse_entry(element: ET.Element) -> Optional[Dict[str, Any]]:
    """Convert one Atom ``<entry>`` into a paper dictionary (None if unusable)."""
    raw_id = (element.findtext(f"{_ATOM}id") or "").strip()
    if "/api/errors" in raw_id:
        message = normalize_space(element.findtext(f"{_ATOM}summary")) or "arXiv rejected the query"
        raise ArxivQueryError(message)
    match = _ARXIV_ID_RE.search(raw_id)
    if not match:
        LOG.debug("Skipping entry with unexpected id %r", raw_id)
        return None
    paper_id = match.group("id")
    title = clean_title(element.findtext(f"{_ATOM}title"))
    published = iso_date(element.findtext(f"{_ATOM}published"))
    updated = iso_date(element.findtext(f"{_ATOM}updated")) or published
    if not (paper_id and title and published):
        LOG.debug("Skipping incomplete entry %r", raw_id)
        return None

    authors = [normalize_space(author.findtext(f"{_ATOM}name")) for author in element.findall(f"{_ATOM}author")]
    categories: List[str] = []
    for category in element.findall(f"{_ATOM}category"):
        term = (category.get("term") or "").strip()
        if _CATEGORY_RE.match(term) and term not in categories:
            categories.append(term)
    primary_element = element.find(f"{_ARXIV}primary_category")
    primary = (primary_element.get("term") or "").strip() if primary_element is not None else ""
    if not _CATEGORY_RE.match(primary):
        primary = categories[0] if categories else ""
    if primary and primary not in categories:
        categories.insert(0, primary)

    return {
        "id": paper_id,
        "title": title,
        "authors": [name for name in authors if name],
        "abstract": normalize_space(element.findtext(f"{_ATOM}summary")),
        "published": published,
        "updated": updated if updated >= published else published,
        "primary": primary,
        "categories": categories,
        "comment": normalize_space(element.findtext(f"{_ARXIV}comment")),
        "journal": normalize_space(element.findtext(f"{_ARXIV}journal_ref")),
        "doi": normalize_space(element.findtext(f"{_ARXIV}doi")),
    }


def parse_feed(content: bytes) -> Tuple[int, int, List[Dict[str, Any]]]:
    """Parse an arXiv API response into ``(total_results, entry_count, papers)``.

    Raises ``xml.etree.ElementTree.ParseError`` for malformed XML and
    ``ArxivQueryError`` when the API reports a query error.
    """
    root = ET.fromstring(content)
    total_text = (root.findtext(f"{_OPENSEARCH}totalResults") or "").strip()
    total = int(total_text) if total_text.isdigit() else 0
    entries = root.findall(f"{_ATOM}entry")
    # The feed title repeats the query as arXiv understood it.
    LOG.debug("arXiv response: %d results in total for %s", total, normalize_space(root.findtext(f"{_ATOM}title")))
    papers = [paper for paper in (parse_entry(entry) for entry in entries) if paper]
    return total, len(entries), papers


class ArxivClient:
    """Minimal, polite client for the arXiv Atom API.

    Enforces the delay required by arXiv's terms of use (one request every three
    seconds, one connection at a time), applies timeouts, retries transient
    failures with exponential backoff and honors ``Retry-After``.
    """

    def __init__(self, *, user_agent: str, page_size: int = 100, delay: float = 3.0,
                 timeout: float = 60.0, max_retries: int = 4,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 api_url: str = ARXIV_API_URL) -> None:
        self.page_size = page_size
        self.delay = delay
        self.timeout = timeout
        self.max_retries = max_retries
        self.api_url = api_url
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._sleep = sleep
        self._clock = clock
        self._last_request: Optional[float] = None

    def search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        """Return up to ``max_results`` papers, newest submissions first."""
        papers: List[Dict[str, Any]] = []
        seen: Set[str] = set()
        start = 0
        empty_pages = 0
        while len(papers) < max_results:
            size = min(self.page_size, max_results - len(papers))
            try:
                total, entry_count, page = self._fetch_page(query, start, size)
            except ArxivQueryError:
                raise
            except ArxivError as exc:
                if not papers:
                    raise
                LOG.warning("Keeping the %d papers already fetched: %s", len(papers), exc)
                break
            if entry_count == 0:
                if start >= total:
                    break
                # The arXiv API occasionally returns an empty page although more
                # results exist (totalResults > start); such pages are retried.
                empty_pages += 1
                if empty_pages > self.max_retries:
                    message = f"arXiv repeatedly returned an empty page at offset {start} of {total}"
                    if not papers:
                        raise ArxivError(message)
                    LOG.warning("%s; keeping the %d papers already fetched", message, len(papers))
                    break
                LOG.warning("Empty result page at offset %d of %d; retrying", start, total)
                self._sleep(self._backoff(empty_pages))
                continue
            empty_pages = 0
            for paper in page:
                if paper["id"] not in seen:
                    seen.add(paper["id"])
                    papers.append(paper)
            start += entry_count
            if start >= total:
                break
        return papers[:max_results]

    def _backoff(self, attempt: int) -> float:
        return min(120.0, 10.0 * 2 ** (attempt - 1))

    def _wait_for_slot(self) -> None:
        if self._last_request is not None:
            remaining = self.delay - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)

    def _fetch_page(self, query: str, start: int, size: int) -> Tuple[int, int, List[Dict[str, Any]]]:
        params = {
            "search_query": query,
            "start": start,
            "max_results": size,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        attempt = 0
        while True:
            self._wait_for_slot()
            retry_after: Optional[float] = None
            try:
                response = self.session.get(self.api_url, params=params, timeout=(15, self.timeout))
            except requests.RequestException as exc:
                problem = f"network error: {exc}"
            else:
                status = response.status_code
                if status == 200:
                    try:
                        return parse_feed(response.content)
                    except ET.ParseError as exc:
                        problem = f"malformed XML response: {exc}"
                elif status in _RETRYABLE_STATUS:
                    problem = f"HTTP {status}"
                    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
                else:
                    message = f"arXiv API returned HTTP {status}"
                    reason = ""
                    try:  # error responses usually carry an Atom entry with the reason
                        parse_feed(response.content)
                    except ArxivQueryError as exc:
                        reason = str(exc)
                    except ET.ParseError:
                        pass
                    if reason or status == 400:  # "Bad Request": the query itself is at fault
                        raise ArxivQueryError(f"{message}: {reason}" if reason else message)
                    raise ArxivError(message)
            finally:
                self._last_request = self._clock()
            attempt += 1
            if attempt > self.max_retries:
                raise ArxivError(f"arXiv API request failed after {attempt} attempts ({problem})")
            wait = retry_after if retry_after is not None else self._backoff(attempt)
            LOG.warning("arXiv request failed (%s); retry %d/%d in %.0f s", problem, attempt, self.max_retries, wait)
            self._sleep(wait)


# ---------------------------------------------------------------------------
# Hugging Face Papers API (code links)
# ---------------------------------------------------------------------------

class HuggingFaceClient:
    """Looks up the GitHub repository Hugging Face links to an arXiv paper.

    Requests are spaced ``delay`` seconds apart. Hugging Face allows anonymous
    clients 500 API requests per five minutes, so the default of 0.7 seconds
    stays below that limit. After a rate-limit response or repeated failures
    the client disables itself and the remaining papers are left for a later run.
    """

    _MAX_FAILURES = 5

    def __init__(self, *, user_agent: str, delay: float = 0.7, timeout: float = 20.0,
                 session: Optional[requests.Session] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.delay = delay
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self._sleep = sleep
        self._clock = clock
        self._last_request: Optional[float] = None
        self._failures = 0
        self.disabled = False
        self.requests_made = 0

    def code_url(self, paper_id: str) -> str:
        if self.disabled:
            return ""
        if self._last_request is not None:
            remaining = self.delay - (self._clock() - self._last_request)
            if remaining > 0:
                self._sleep(remaining)
        self.requests_made += 1
        try:
            response = self.session.get(HF_PAPER_API_URL.format(paper_id), timeout=(10, self.timeout))
        except requests.RequestException as exc:
            self._failed(f"network error: {exc}")
            return ""
        finally:
            self._last_request = self._clock()
        if response.status_code == 404:  # paper not indexed on Hugging Face
            self._failures = 0
            return ""
        if response.status_code == 429:
            LOG.warning("Hugging Face rate limit reached; skipping the remaining code-link lookups")
            self.disabled = True
            return ""
        if response.status_code != 200:
            self._failed(f"HTTP {response.status_code}")
            return ""
        self._failures = 0
        try:
            data = response.json()
        except ValueError:
            return ""
        repo = data.get("githubRepo") if isinstance(data, dict) else None
        return extract_code_url(repo) if isinstance(repo, str) else ""

    def _failed(self, reason: str) -> None:
        self._failures += 1
        if self._failures >= self._MAX_FAILURES:
            self.disabled = True
            LOG.warning("Hugging Face API unavailable (%s); skipping the remaining code-link lookups", reason)


# ---------------------------------------------------------------------------
# Paper database
# ---------------------------------------------------------------------------

#: Field order of a stored paper record. Optional fields are omitted when empty.
_RECORD_FIELDS = ("id", "title", "authors", "published", "updated", "primary", "categories",
                  "comment", "journal", "doi", "code", "topics", "added")
_OPTIONAL_FIELDS = frozenset({"comment", "journal", "doi", "code"})


def dumps_json_lines(payload: Dict[str, Any], line_keys: Sequence[str] = ("topics", "papers")) -> str:
    """Serialize compactly with one list item per line (small, diff-friendly JSON)."""
    def compact(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    parts = []
    for key, value in payload.items():
        if key in line_keys and isinstance(value, list):
            body = ",\n".join(compact(item) for item in value)
            parts.append(f"{compact(key)}:[\n{body}\n]" if value else f"{compact(key)}:[]")
        else:
            parts.append(f"{compact(key)}:{compact(value)}")
    return "{\n" + ",\n".join(parts) + "\n}\n"


def write_if_changed(path: Path, text: str) -> bool:
    """Atomically write UTF-8 text with LF line endings; skip identical content."""
    path = Path(path)
    try:
        if path.read_text(encoding="utf-8") == text:
            return False
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(temporary, path)
    return True


class PaperStore:
    """Paper metadata (``papers.json``) plus abstracts sharded by arXiv month."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = Path(data_dir)
        self.papers: Dict[str, Dict[str, Any]] = {}
        self.generated = ""
        self._original_text: Optional[str] = None
        self._abstracts: Dict[str, Dict[str, str]] = {}
        self._dirty_shards: Set[str] = set()

    @property
    def database_path(self) -> Path:
        return self.data_dir / "papers.json"

    @property
    def abstracts_dir(self) -> Path:
        return self.data_dir / "abstracts"

    @classmethod
    def load(cls, data_dir: Path) -> "PaperStore":
        store = cls(data_dir)
        path = store.database_path
        if not path.exists():
            return store
        try:
            text = path.read_text(encoding="utf-8")
            payload = json.loads(text)
        except (OSError, UnicodeDecodeError, ValueError) as exc:
            raise StoreError(f"Cannot parse {path}: {exc}. Repair the file (or restore it from git) and rerun.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("papers"), list):
            raise StoreError(f"{path} is not a paper database (expected an object with a 'papers' list)")
        if payload.get("schema") != SCHEMA_VERSION:
            raise StoreError(f"{path} uses schema {payload.get('schema')!r}; this script expects {SCHEMA_VERSION}")
        store._original_text = text.replace("\r\n", "\n")
        store.generated = payload.get("generated") if isinstance(payload.get("generated"), str) else ""
        for record in payload["papers"]:
            if isinstance(record, dict) and isinstance(record.get("id"), str) and record["id"]:
                topics = record.get("topics")
                record["topics"] = [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else []
                store.papers[record["id"]] = record
        return store

    # -- abstracts ---------------------------------------------------------
    def _shard(self, key: str) -> Dict[str, str]:
        if key not in self._abstracts:
            path = self.abstracts_dir / f"{key}.json"
            shard: Dict[str, str] = {}
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, ValueError) as exc:
                    LOG.warning("Ignoring unreadable abstract file %s (%s); it will be regenerated", path, exc)
                    loaded = {}
                if isinstance(loaded, dict):
                    shard = {k: v for k, v in loaded.items() if isinstance(k, str) and isinstance(v, str)}
            self._abstracts[key] = shard
        return self._abstracts[key]

    def abstract(self, paper_id: str) -> str:
        return self._shard(shard_key(paper_id)).get(paper_id, "")

    def set_abstract(self, paper_id: str, text: str) -> None:
        if not text:
            return
        key = shard_key(paper_id)
        shard = self._shard(key)
        if shard.get(paper_id) != text:
            shard[paper_id] = text
            self._dirty_shards.add(key)

    def _drop_abstract(self, paper_id: str) -> None:
        key = shard_key(paper_id)
        shard = self._shard(key)
        if paper_id in shard:
            del shard[paper_id]
            self._dirty_shards.add(key)

    # -- records -----------------------------------------------------------
    def upsert(self, entry: Dict[str, Any], topic_id: str, today: str, code_url: str = "") -> Tuple[bool, bool]:
        """Insert or refresh a paper. Returns ``(new_paper, new_topic_assignment)``."""
        paper_id = entry["id"]
        record = self.papers.get(paper_id)
        new_paper = record is None
        if record is None:
            record = {"id": paper_id, "topics": [], "added": today}
            self.papers[paper_id] = record
        for key in ("title", "authors", "published", "updated", "primary", "categories"):
            record[key] = entry[key]
        for key in ("comment", "journal", "doi"):
            if entry.get(key):
                record[key] = entry[key]
            else:
                record.pop(key, None)
        if code_url:
            record["code"] = code_url
        new_assignment = topic_id not in record["topics"]
        if new_assignment:
            record["topics"].append(topic_id)
        self.set_abstract(paper_id, entry.get("abstract", ""))
        return new_paper, new_assignment

    def prune(self, topic_ids: Sequence[str], retention_days: int, today: dt.date) -> int:
        """Drop unknown topic ids, papers without topics and papers past retention."""
        valid = set(topic_ids)
        cutoff = (today - dt.timedelta(days=retention_days)).isoformat() if retention_days > 0 else ""
        removed = []
        for paper_id, record in list(self.papers.items()):
            record["topics"] = [topic for topic in record.get("topics", []) if topic in valid]
            if not record["topics"] or (cutoff and record.get("published", "") < cutoff):
                removed.append(paper_id)
                del self.papers[paper_id]
        for paper_id in removed:
            self._drop_abstract(paper_id)
        return len(removed)

    def records(self, topic_order: Sequence[str]) -> List[Dict[str, Any]]:
        """Clean, ordered records: newest submission first, then by arXiv id."""
        rank = {topic: index for index, topic in enumerate(topic_order)}
        result = []
        ordered = sorted(self.papers.values(), key=lambda r: (r.get("published", ""), r["id"]), reverse=True)
        for record in ordered:
            clean: Dict[str, Any] = {}
            for key in _RECORD_FIELDS:
                value = record.get(key)
                if key in _OPTIONAL_FIELDS and not value:
                    continue
                if key == "topics":
                    value = sorted(value or [], key=lambda topic: rank.get(topic, len(rank)))
                elif key in ("authors", "categories"):
                    value = list(value or [])
                elif value is None:
                    value = ""
                clean[key] = value
            result.append(clean)
        return result

    def save(self, topics_payload: List[Dict[str, Any]], topic_order: Sequence[str],
             now: dt.datetime) -> Tuple[List[Dict[str, Any]], List[Path]]:
        """Write papers.json and changed abstract files. Returns ``(records, changed_paths)``."""
        records = self.records(topic_order)
        changed: List[Path] = []

        def render(generated: str) -> str:
            return dumps_json_lines({"schema": SCHEMA_VERSION, "generated": generated,
                                     "topics": topics_payload, "papers": records})

        text = render(self.generated)
        if records and (not self.generated or text != self._original_text):
            # Only bump the timestamp when the paper data really changed (an
            # empty database keeps "" so the site reports "awaiting update").
            self.generated = format_timestamp(now)
            text = render(self.generated)
        if write_if_changed(self.database_path, text):
            changed.append(self.database_path)
        self._original_text = text

        for key in sorted(self._dirty_shards):
            path = self.abstracts_dir / f"{key}.json"
            shard = self._abstracts.get(key, {})
            if shard:
                body = json.dumps(dict(sorted(shard.items())), ensure_ascii=False, indent=0) + "\n"
                if write_if_changed(path, body):
                    changed.append(path)
            elif path.exists():
                path.unlink()
                changed.append(path)
        self._dirty_shards.clear()
        return records, changed


# ---------------------------------------------------------------------------
# Site outputs
# ---------------------------------------------------------------------------

def topics_payload(settings: Settings) -> List[Dict[str, Any]]:
    return [{"id": topic.id, "name": topic.name, "description": topic.description} for topic in settings.topics]


def render_topics_data(settings: Settings) -> str:
    data = [
        {"id": topic.id, "name": topic.name, "description": topic.description,
         "filters": list(topic.filters), "categories": list(topic.categories)}
        for topic in settings.topics
    ]
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def render_site_page(settings: Settings, records: Sequence[Dict[str, Any]], generated: str) -> str:
    """Front matter for docs/index.md; the page body is rendered by the papers layout."""
    count = len(records)
    description = (f"{settings.description} {count:,} papers across {len(settings.topics)} topics."
                   if count else settings.description)
    lines = [
        "---",
        "# Generated by daily_arxiv.py. Do not edit by hand: changes are overwritten.",
        "layout: papers",
        "title: Papers",
        "nav_order: 1",
        "permalink: /",
        f"description: {json.dumps(description, ensure_ascii=False)}",
        f"last_updated: {json.dumps(generated[:10])}",
        f"paper_count: {count}",
        f"topic_count: {len(settings.topics)}",
        "---",
        "",
    ]
    return "\n".join(lines)


def render_feed(settings: Settings, store: PaperStore, records: Sequence[Dict[str, Any]], generated: str) -> str:
    """Atom feed of the most recently added papers."""
    def sub(parent: ET.Element, tag: str, text: str, attrib: Optional[Dict[str, str]] = None) -> ET.Element:
        element = ET.SubElement(parent, tag, attrib or {})
        element.text = text
        return element

    names = {topic.id: topic.name for topic in settings.topics}
    feed = ET.Element("feed", {"xmlns": "http://www.w3.org/2005/Atom"})
    sub(feed, "title", settings.title)
    sub(feed, "subtitle", settings.description)
    ET.SubElement(feed, "link", {"href": settings.site_url + "feed.xml", "rel": "self",
                                 "type": "application/atom+xml"})
    ET.SubElement(feed, "link", {"href": settings.site_url, "rel": "alternate", "type": "text/html"})
    sub(feed, "id", settings.site_url)
    # Deterministic output: an empty database keeps a fixed timestamp.
    sub(feed, "updated", generated or "1970-01-01T00:00:00Z")
    sub(ET.SubElement(feed, "author"), "name", settings.title)
    sub(feed, "generator", "daily_arxiv.py", {"uri": f"https://github.com/{settings.repository}",
                                              "version": __version__})
    latest = sorted(records, key=lambda r: (r["added"], r["published"], r["id"]), reverse=True)
    for record in latest[:settings.feed_size]:
        entry = ET.SubElement(feed, "entry")
        abs_url = ARXIV_ABS_URL.format(record["id"])
        sub(entry, "id", abs_url)
        sub(entry, "title", record["title"])
        ET.SubElement(entry, "link", {"href": abs_url, "rel": "alternate", "type": "text/html"})
        ET.SubElement(entry, "link", {"href": ARXIV_PDF_URL.format(record["id"]), "rel": "related",
                                      "type": "application/pdf", "title": "PDF"})
        if record.get("code"):
            ET.SubElement(entry, "link", {"href": record["code"], "rel": "related", "title": "Code"})
        sub(entry, "published", f"{record['published']}T00:00:00Z")
        sub(entry, "updated", f"{max(record['added'], record['published'])}T00:00:00Z")
        for author in record["authors"] or ["Unknown"]:
            sub(ET.SubElement(entry, "author"), "name", author)
        for topic in record["topics"]:
            ET.SubElement(entry, "category", {"term": topic, "label": names.get(topic, topic)})
        summary = store.abstract(record["id"])
        if summary:
            sub(entry, "summary", summary, {"type": "text"})
    ET.indent(feed, space="  ")
    return '<?xml version="1.0" encoding="utf-8"?>\n' + ET.tostring(feed, encoding="unicode") + "\n"


def write_outputs(settings: Settings, store: PaperStore, now: dt.datetime) -> List[Path]:
    """Persist the database and regenerate every derived site file."""
    records, changed = store.save(topics_payload(settings), settings.topic_ids, now)
    outputs = {
        settings.topics_data: render_topics_data(settings),
        settings.site_page: render_site_page(settings, records, store.generated),
        settings.feed_path: render_feed(settings, store, records, store.generated),
    }
    for path, text in outputs.items():
        if write_if_changed(path, text):
            changed.append(path)
    return changed


# ---------------------------------------------------------------------------
# GitHub Actions integration
# ---------------------------------------------------------------------------

def annotate(level: str, message: str) -> None:
    """Emit a GitHub Actions annotation (no-op outside Actions)."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        escaped = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::{level}::{escaped}", flush=True)


def write_step_summary(lines: Sequence[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n\n")


def _relative(settings: Settings, paths: Iterable[Path]) -> List[str]:
    result = []
    for path in paths:
        try:
            result.append(Path(path).resolve().relative_to(settings.root).as_posix())
        except ValueError:
            result.append(str(path))
    return result


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

#: After this many consecutive topics fail with network or server errors, the run
#: stops querying: arXiv is evidently unavailable, and sitting through the retries
#: of every remaining topic would only delay saving what was already fetched.
_MAX_CONSECUTIVE_FAILURES = 2
#: New papers listed per topic in the log of a regular run (dry runs list everything).
_LOG_PAPERS_PER_TOPIC = 20


def _log_papers(entries: Sequence[Dict[str, Any]], new_ids: Set[str], *, show_all: bool) -> None:
    """List fetched papers in the log; ``+`` marks papers that are new to the database."""
    listed = list(entries) if show_all else [entry for entry in entries if entry["id"] in new_ids]
    shown = listed if show_all else listed[:_LOG_PAPERS_PER_TOPIC]
    for entry in shown:
        marker = "+" if entry["id"] in new_ids else " "
        LOG.info("  %s %s  %-12s %s", marker, entry["published"], entry["id"], entry["title"])
    if len(listed) > len(shown):
        LOG.info("    ... and %d more new papers", len(listed) - len(shown))


def link_code_from_huggingface(store: PaperStore, client: HuggingFaceClient, paper_ids: Sequence[str], *,
                               time_limit: Optional[float] = None,
                               clock: Callable[[], float] = time.monotonic) -> int:
    """Ask Hugging Face for the repositories of ``paper_ids``, in the given order.

    Returns the number of links found. The lookups end early when the client
    disables itself or after ``time_limit`` seconds, so that a large backfill
    cannot run into the time limit of the workflow.
    """
    found = 0
    started = clock()
    for position, paper_id in enumerate(paper_ids):
        if client.disabled:
            break
        if time_limit is not None and clock() - started >= time_limit:
            LOG.warning("Hugging Face lookups stopped after %.0f minutes (code_links.time_limit_minutes); "
                        "%d papers were not checked in this run", time_limit / 60, len(paper_ids) - position)
            break
        record = store.papers.get(paper_id)
        if record is None or record.get("code"):
            continue
        url = client.code_url(paper_id)
        if url:
            record["code"] = url
            found += 1
    return found


def run_update(settings: Settings, *, max_results: Optional[int] = None,
               topics: Optional[Sequence[Topic]] = None, dry_run: bool = False,
               client: Optional[ArxivClient] = None,
               hf_client: Optional[HuggingFaceClient] = None,
               now: Optional[dt.datetime] = None) -> int:
    """Fetch new papers for each topic and regenerate the site data."""
    now = now or utc_now()
    today = now.date().isoformat()
    limit = max_results or settings.max_results
    selected = list(topics or settings.topics)
    store = PaperStore.load(settings.data_dir)
    known_before = len(store.papers)
    client = client or ArxivClient(user_agent=settings.user_agent, page_size=settings.page_size,
                                   delay=settings.request_delay, timeout=settings.timeout,
                                   max_retries=settings.max_retries)

    rows: List[str] = []
    new_papers: List[str] = []
    failed: List[str] = []
    consecutive_failures = 0
    for position, topic in enumerate(selected):
        LOG.info("[%s] requesting the %d newest matching papers", topic.name, limit)
        try:
            entries = client.search(topic.query, limit)
        except ArxivError as exc:
            failed.append(topic.name)
            LOG.error("[%s] arXiv query failed: %s", topic.name, exc)
            annotate("error", f"{topic.name}: arXiv query failed: {exc}")
            rows.append(f"| {topic.name} | - | - | failed |")
            # A rejected query concerns one topic only; any other error points at arXiv itself.
            consecutive_failures = 0 if isinstance(exc, ArxivQueryError) else consecutive_failures + 1
            remaining = selected[position + 1:]
            if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES and remaining:
                LOG.error("arXiv appears to be unavailable; skipping the remaining %d topic(s)", len(remaining))
                failed.extend(skipped.name for skipped in remaining)
                rows.extend(f"| {skipped.name} | - | - | skipped |" for skipped in remaining)
                break
            continue
        consecutive_failures = 0
        fresh: Set[str] = set()
        assigned = 0
        for entry in entries:
            code = extract_code_url(entry["abstract"], entry["comment"]) if settings.code_from_abstract else ""
            is_new, is_assigned = store.upsert(entry, topic.id, today, code_url=code)
            if is_new:
                new_papers.append(entry["id"])
                fresh.add(entry["id"])
            assigned += int(is_assigned)
        LOG.info("[%s] %d papers returned: %d new, %d newly assigned to this topic",
                 topic.name, len(entries), len(fresh), assigned)
        _log_papers(entries, fresh, show_all=dry_run)
        if not entries:
            LOG.warning("[%s] the query matched no papers; check this topic's search phrases", topic.name)
            annotate("warning", f"{topic.name}: the arXiv query matched no papers; check its search phrases")
        rows.append(f"| {topic.name} | {len(entries)} | {assigned} | ok |")

    if len(failed) == len(selected):
        LOG.error("Every arXiv query failed; the existing data was left untouched.")
        return 1

    code_found = 0
    if settings.code_huggingface and not dry_run and new_papers:
        hf_client = hf_client or HuggingFaceClient(user_agent=settings.user_agent,
                                                   delay=settings.code_request_delay)
        # Newest papers first: they matter most if the lookups end early.
        pending = sorted((paper_id for paper_id in new_papers if not store.papers[paper_id].get("code")),
                         key=lambda paper_id: (store.papers[paper_id]["published"], paper_id), reverse=True)
        code_found = link_code_from_huggingface(store, hf_client, pending, time_limit=settings.code_time_limit)
    removed = store.prune(settings.topic_ids, settings.retention_days, now.date())

    with_code = sum(1 for paper_id in new_papers if store.papers.get(paper_id, {}).get("code"))
    LOG.info("New papers: %d (%d with code, %d via Hugging Face). Removed: %d. Database: %d -> %d papers.",
             len(new_papers), with_code, code_found, removed, known_before, len(store.papers))
    if failed:
        LOG.warning("%d topic(s) could not be updated and will be retried on the next run: %s",
                    len(failed), ", ".join(failed))
        annotate("warning", f"{len(failed)} topic(s) could not be updated and will be retried on the next run: "
                            f"{', '.join(failed)}")

    if dry_run:
        LOG.info("Dry run: no files were written.")
        return 0
    changed = write_outputs(settings, store, now)
    LOG.info("Updated files: %s", ", ".join(_relative(settings, changed)) or "none (no changes)")
    write_step_summary([
        "## Image Retrieval arXiv Daily",
        f"New papers: **{len(new_papers)}** ({with_code} with code) - database size: **{len(store.papers)}**",
        "",
        "| Topic | Returned | Newly assigned | Status |",
        "|---|---:|---:|---|",
        *rows,
    ])
    return 0


def run_link_refresh(settings: Settings, *, dry_run: bool = False,
                     hf_client: Optional[HuggingFaceClient] = None,
                     now: Optional[dt.datetime] = None) -> int:
    """Re-check code links for recent papers that do not have one yet."""
    now = now or utc_now()
    store = PaperStore.load(settings.data_dir)
    cutoff = (now.date() - dt.timedelta(days=settings.code_refresh_days)).isoformat()
    candidates = [record["id"] for record in store.records(settings.topic_ids)
                  if not record.get("code") and record["published"] >= cutoff]
    LOG.info("%d papers published since %s have no code link", len(candidates), cutoff)

    from_text = 0
    if settings.code_from_abstract:
        for paper_id in candidates:
            record = store.papers[paper_id]
            url = extract_code_url(store.abstract(paper_id), record.get("comment"))
            if url:
                record["code"] = url
                from_text += 1
    from_hf = 0
    if settings.code_huggingface and not dry_run:
        hf_client = hf_client or HuggingFaceClient(user_agent=settings.user_agent,
                                                   delay=settings.code_request_delay)
        remaining = [paper_id for paper_id in candidates if not store.papers[paper_id].get("code")]
        from_hf = link_code_from_huggingface(store, hf_client, remaining, time_limit=settings.code_time_limit)
    LOG.info("Code links found: %d from abstracts/comments, %d via Hugging Face", from_text, from_hf)
    removed = store.prune(settings.topic_ids, settings.retention_days, now.date())
    if removed:
        LOG.info("Removed %d papers (topic no longer configured or past retention)", removed)
    if dry_run:
        LOG.info("Dry run: no files were written.")
        return 0
    changed = write_outputs(settings, store, now)
    LOG.info("Updated files: %s", ", ".join(_relative(settings, changed)) or "none (no changes)")
    write_step_summary([
        "## Code-link refresh",
        f"Checked **{len(candidates)}** papers without code: {from_text} links from abstracts/comments, "
        f"{from_hf} via Hugging Face.",
    ])
    return 0


def run_rebuild(settings: Settings, *, dry_run: bool = False, now: Optional[dt.datetime] = None) -> int:
    """Regenerate every output from the stored database (no network access)."""
    now = now or utc_now()
    store = PaperStore.load(settings.data_dir)
    removed = store.prune(settings.topic_ids, settings.retention_days, now.date())
    LOG.info("Rebuilding outputs for %d papers (%d removed)", len(store.papers), removed)
    if dry_run:
        return 0
    changed = write_outputs(settings, store, now)
    LOG.info("Updated files: %s", ", ".join(_relative(settings, changed)) or "none (no changes)")
    return 0


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {value!r}") from None
    if not 1 <= number <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Image Retrieval arXiv Daily: fetch, organize and publish the latest image retrieval papers.")
    parser.add_argument("--config_path", "--config", default="config.yaml",
                        help="configuration file (default: config.yaml)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update_paper_links", action="store_true",
                      help="re-check code links of recent papers instead of fetching new ones")
    mode.add_argument("--rebuild", action="store_true",
                      help="regenerate site files from the stored data without network access")
    parser.add_argument("--max_results", type=_positive_int, metavar="N",
                        help="newest papers to request per topic (overrides config.yaml)")
    parser.add_argument("--topic", action="append", metavar="ID",
                        help="only update this topic id (repeatable)")
    parser.add_argument("--dry_run", action="store_true", help="run without writing any files")
    parser.add_argument("--verbose", "-v", action="store_true", help="show debug logging")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if (args.rebuild or args.update_paper_links) and (args.topic or args.max_results):
        parser.error("--topic and --max_results only apply when fetching papers "
                     "(not with --rebuild or --update_paper_links)")
    for stream in (sys.stdout, sys.stderr):
        try:  # never crash on consoles that cannot encode paper titles
            stream.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(format="[%(asctime)s %(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S",
                        level=logging.DEBUG if args.verbose else logging.INFO)
    try:
        settings = load_settings(Path(args.config_path))
        topics = None
        if args.topic:
            by_id = {topic.id: topic for topic in settings.topics}
            unknown = [topic_id for topic_id in args.topic if topic_id not in by_id]
            if unknown:
                raise ConfigError(f"Unknown topic id(s): {', '.join(unknown)}. "
                                  f"Available: {', '.join(settings.topic_ids)}")
            topics = [by_id[topic_id] for topic_id in dict.fromkeys(args.topic)]
        if args.rebuild:
            return run_rebuild(settings, dry_run=args.dry_run)
        if args.update_paper_links:
            return run_link_refresh(settings, dry_run=args.dry_run)
        return run_update(settings, max_results=args.max_results, topics=topics, dry_run=args.dry_run)
    except (ConfigError, StoreError) as exc:
        LOG.error("%s", exc)
        annotate("error", str(exc))
        return 1
    except KeyboardInterrupt:
        LOG.error("Interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
