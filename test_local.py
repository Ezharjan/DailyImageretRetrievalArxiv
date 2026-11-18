#!/usr/bin/env python3
"""Tests for Image Retrieval arXiv Daily.

    python test_local.py            # offline test suite (no network, about a second)
    python test_local.py --live     # then query the real arXiv API once per topic (~1 minute)
    python test_local.py -v         # verbose test names

The offline suite never touches the repository data: every test that writes
files works in a temporary directory.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import doctest
import io
import json
import logging
import os
import sys
import tempfile
import textwrap
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import daily_arxiv as da  # noqa: E402  (import after sys.path tweak)

NOW = dt.datetime(2026, 11, 18, 4, 41, 7, tzinfo=dt.timezone.utc)
LATER = dt.datetime(2026, 11, 19, 4, 40, 0, tzinfo=dt.timezone.utc)


def setUpModule() -> None:
    """Keep simulated failures out of the annotations and job summary of a CI run."""
    for variable in ("GITHUB_ACTIONS", "GITHUB_STEP_SUMMARY"):
        os.environ.pop(variable, None)


def load_tests(loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: Optional[str]) -> unittest.TestSuite:
    """Also run the examples in the docstrings of daily_arxiv.py."""
    tests.addTests(doctest.DocTestSuite(da))
    return tests


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def atom_entry(paper_id: str, title: str, *, version: int = 1, published: str = "2026-11-16T17:59:59Z",
               updated: Optional[str] = None, summary: str = "We study image retrieval.",
               authors: Optional[List[str]] = None, comment: str = "", primary: str = "cs.CV",
               categories: Optional[List[str]] = None) -> str:
    authors = authors or ["Alice Zhang", "Bob Müller"]
    categories = categories or [primary]
    parts = [
        "<entry>",
        f"<id>http://arxiv.org/abs/{paper_id}v{version}</id>",
        f"<updated>{updated or published}</updated>",
        f"<published>{published}</published>",
        f"<title>{title}</title>",
        f"<summary>{summary}</summary>",
        *[f"<author><name>{name}</name></author>" for name in authors],
        f"<arxiv:comment>{comment}</arxiv:comment>" if comment else "",
        f'<link href="http://arxiv.org/abs/{paper_id}v{version}" rel="alternate" type="text/html"/>',
        f'<link title="pdf" href="http://arxiv.org/pdf/{paper_id}v{version}" rel="related" type="application/pdf"/>',
        f'<arxiv:primary_category term="{primary}" scheme="http://arxiv.org/schemas/atom"/>',
        *[f'<category term="{term}" scheme="http://arxiv.org/schemas/atom"/>' for term in categories],
        "</entry>",
    ]
    return "\n".join(part for part in parts if part)


def atom_feed(entries: List[str], total: Optional[int] = None, start: int = 0) -> bytes:
    total = len(entries) if total is None else total
    return textwrap.dedent(f"""\
        <?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"
              xmlns:arxiv="http://arxiv.org/schemas/atom">
          <id>https://arxiv.org/api/test</id>
          <title>arXiv Query: test</title>
          <updated>2026-11-18T00:00:00-05:00</updated>
          <opensearch:itemsPerPage>{len(entries)}</opensearch:itemsPerPage>
          <opensearch:totalResults>{total}</opensearch:totalResults>
          <opensearch:startIndex>{start}</opensearch:startIndex>
        """).encode("utf-8") + "\n".join(entries).encode("utf-8") + b"\n</feed>\n"


ERROR_FEED = atom_feed([textwrap.dedent("""\
    <entry>
      <id>http://arxiv.org/api/errors#unbalanced_parentheses</id>
      <title>Error</title>
      <summary>unbalanced parentheses in query</summary>
      <updated>2026-11-18T00:00:00-05:00</updated>
      <author><name>arXiv api core</name></author>
    </entry>""")])

SAMPLE_ENTRY = atom_entry(
    "2611.01234", "Zero-Shot Composed Image Retrieval\n   with \\&amp; Diffusion  Priors", version=2,
    published="2026-11-16T17:59:59Z", updated="2026-11-17T18:00:00Z",
    summary="  We study composed image retrieval (CIR).\nCode is available at\nhttps://github.com/example-lab/ZS-CIR.git.\n",
    comment="Accepted to CVPR 2026; 10 pages", primary="cs.CV", categories=["cs.CV", "cs.IR", "I.4.8", "68T45"])


class FakeResponse:
    def __init__(self, status: int = 200, content: bytes = b"", headers: Optional[Dict[str, str]] = None,
                 payload: Any = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._payload = payload
        self.text = content.decode("utf-8", "replace")

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no JSON")
        return self._payload


class FakeSession:
    """Replays a queue of responses (or exceptions) and records requests."""

    def __init__(self, responses: List[Any]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []
        self.headers: Dict[str, str] = {}

    def get(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: Any = None) -> FakeResponse:
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(responses: List[Any], **kwargs: Any):
    clock = FakeClock()
    session = FakeSession(responses)
    client = da.ArxivClient(user_agent="test", session=session, sleep=clock.sleep, clock=clock, **kwargs)
    return client, session, clock


class FakeArxiv:
    """Stands in for ArxivClient in end-to-end tests: query -> list of entries."""

    def __init__(self, results: Dict[str, List[Dict[str, Any]]], failing: Optional[set] = None,
                 rejected: Optional[set] = None) -> None:
        self.results = results
        self.failing = failing or set()      # queries that hit a network/server error
        self.rejected = rejected or set()    # queries arXiv refuses as malformed
        self.queries: List[str] = []

    def search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
        self.queries.append(query)
        if query in self.rejected:
            raise da.ArxivQueryError("simulated syntax error")
        if query in self.failing:
            raise da.ArxivError("simulated outage")
        return [dict(entry) for entry in self.results.get(query, [])][:max_results]


class FakeHF:
    def __init__(self, repos: Optional[Dict[str, str]] = None) -> None:
        self.repos = repos or {}
        self.disabled = False
        self.asked: List[str] = []

    def code_url(self, paper_id: str) -> str:
        self.asked.append(paper_id)
        return self.repos.get(paper_id, "")


def entry(paper_id: str, title: str, published: str = "2026-11-16", **extra: Any) -> Dict[str, Any]:
    base = {"id": paper_id, "title": title, "authors": ["Alice Zhang"], "abstract": f"Abstract of {title}.",
            "published": published, "updated": published, "primary": "cs.CV", "categories": ["cs.CV"],
            "comment": "", "journal": "", "doi": ""}
    base.update(extra)
    return base


TEST_CONFIG = """
site:
  title: "Test Tracker"
  description: "Test description."
  url: "https://example.github.io/test-repo"
  repository: "example/test-repo"
max_results: 50
arxiv:
  categories: [cs.CV, cs.IR]
code_links:
  huggingface: true
output:
  data_dir: out/data
  site_page: out/index.md
  topics_data: out/_data/topics.json
  feed: out/feed.xml
  feed_size: 10
keywords:
  "Composed Image Retrieval":
    id: composed
    description: "Reference image plus text."
    filters: ["composed image retrieval", 'ti:"text feedback"']
  "Sketch-Based Image Retrieval":
    description: "Sketch queries."
    categories: [cs.CV]
    filters: ["sketch-based image retrieval"]
"""


class TempProject(unittest.TestCase):
    """Base class: a throwaway project folder with its own config.yaml."""

    config_text = TEST_CONFIG

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "config.yaml"
        self.config_path.write_text(self.config_text, encoding="utf-8")
        self.settings = da.load_settings(self.config_path)
        self.q_composed = self.settings.topics[0].query
        self.q_sketch = self.settings.topics[1].query

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def database(self) -> Dict[str, Any]:
        return json.loads((self.root / "out/data/papers.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TextHelperTests(unittest.TestCase):
    def test_normalize_space_and_titles(self):
        self.assertEqual(da.normalize_space("  a\n\tb \x0c c\x07 "), "a b c")
        self.assertEqual(da.clean_title("Retrieval \\& Ranking at 100\\% Recall"), "Retrieval & Ranking at 100% Recall")
        self.assertEqual(da.normalize_space(None), "")

    def test_slugify(self):
        self.assertEqual(da.slugify("Image-Text & Cross-Modal Retrieval"), "image-text-cross-modal-retrieval")
        self.assertEqual(da.slugify("  Re-ID (Person) été "), "re-id-person-ete")

    def test_iso_date(self):
        self.assertEqual(da.iso_date("2026-11-16T17:59:59Z"), "2026-11-16")
        self.assertEqual(da.iso_date("2026-02-30T00:00:00Z"), "")
        self.assertEqual(da.iso_date("yesterday"), "")

    def test_shard_key(self):
        self.assertEqual(da.shard_key("2611.01234"), "2611")
        self.assertEqual(da.shard_key("0704.0001"), "0704")
        self.assertEqual(da.shard_key("cs/0601001"), "legacy")


class CodeLinkTests(unittest.TestCase):
    def test_extracts_and_normalizes_repositories(self):
        cases = {
            "Code: https://github.com/foo/bar.": "https://github.com/foo/bar",
            "see (https://github.com/Foo-Lab/My.Repo.git), and more": "https://github.com/Foo-Lab/My.Repo",
            "at github.com/foo/bar/tree/main/src": "https://github.com/foo/bar",
            "https://www.github.com/foo/bar-baz_2": "https://github.com/foo/bar-baz_2",
            "GitLab: https://gitlab.com/group/project": "https://gitlab.com/group/project",
            "Code under review: https://anonymous.4open.science/r/CIR-1234/": "https://anonymous.4open.science/r/CIR-1234",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(da.extract_code_url(text), expected)

    def test_ignores_non_repository_links(self):
        for text in ("https://github.com/features/copilot", "https://example.github.io/project/",
                     "https://github.com/onlyowner", "visit notgithub.com/foo/bar", "", None):
            with self.subTest(text=text):
                self.assertEqual(da.extract_code_url(text), "")

    def test_prefers_first_text_and_github_over_anonymous(self):
        self.assertEqual(da.extract_code_url("anonymous.4open.science/r/x github.com/a/b"), "https://github.com/a/b")
        self.assertEqual(da.extract_code_url("", "comment: github.com/c/d"), "https://github.com/c/d")


class QueryTests(unittest.TestCase):
    def test_phrases_fields_and_categories(self):
        query = da.build_query(['sketch-based image retrieval', 'ti:"image search"'], ["ti", "abs"], ["cs.CV", "cs.IR"])
        self.assertEqual(query, '(ti:"sketch based image retrieval" OR abs:"sketch based image retrieval" OR '
                                'ti:"image search") AND (cat:cs.CV OR cat:cs.IR)')

    def test_raw_filters_are_grouped_only_when_needed(self):
        cases = {
            ('ti:"image search"',): '(ti:"image search")',
            ('abs:hashing ANDNOT abs:"hash table"', "deep hashing"):
                '((abs:hashing ANDNOT abs:"hash table") OR ti:"deep hashing")',
            ('(ti:a OR ti:b)',): '(ti:a OR ti:b)',                       # already grouped: no second pair
            ('(ti:a) OR (ti:b)', 'au:smith'): '(((ti:a) OR (ti:b)) OR au:smith)',
        }
        for filters, expected in cases.items():
            with self.subTest(filters=filters):
                self.assertEqual(da.build_query(filters, ["ti"], []), expected)

    def test_hyphens_become_spaces_in_plain_phrases(self):
        # The arXiv API mishandles hyphenated query terms, so they are never sent.
        dashes = [chr(code) for code in (0x2D, 0x2010, 0x2011, 0x2013, 0x2014, 0x2212)]
        for dash in dashes:
            with self.subTest(dash=hex(ord(dash))):
                self.assertEqual(da.sanitize_phrase(f"re{dash}identification"), "re identification")
        self.assertEqual(da.build_query(["text-to-image retrieval"], ["ti"], []), '(ti:"text to image retrieval")')
        # Single words are quoted too, and category names keep their hyphens.
        self.assertEqual(da.build_query(["re-ID"], ["abs"], ["cond-mat.stat-mech"]),
                         '(abs:"re ID") AND (cat:cond-mat.stat-mech)')

    def test_phrase_sanitizing(self):
        self.assertEqual(da.sanitize_phrase('ColPali: "late" \\interaction'), "ColPali late interaction")
        self.assertTrue(da.is_raw_filter('abs:"x" AND cat:cs.CV'))
        self.assertTrue(da.is_raw_filter("(a OR b)"))
        self.assertFalse(da.is_raw_filter("re-identification"))
        self.assertFalse(da.is_raw_filter("ColPali: late interaction"))


class ConfigTests(TempProject):
    def write(self, text: str) -> Path:
        path = self.root / "bad.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_loads_and_resolves_paths(self):
        s = self.settings
        self.assertEqual(s.site_url, "https://example.github.io/test-repo/")
        self.assertEqual(s.data_dir, (self.root / "out/data").resolve())
        self.assertEqual([t.id for t in s.topics], ["composed", "sketch-based-image-retrieval"])
        self.assertEqual(s.topics[1].categories, ("cs.CV",))
        self.assertEqual(s.request_delay, 3.0)
        self.assertEqual((s.code_request_delay, s.code_time_limit), (0.7, 1800.0))   # defaults

    def test_rejects_invalid_settings(self):
        base = yaml.safe_load(TEST_CONFIG)
        broken = {
            "duplicate id": {"keywords": {"A": {"id": "x", "filters": ["a"]}, "B": {"id": "x", "filters": ["b"]}}},
            "empty filters": {"keywords": {"A": {"filters": []}}},
            "bad category": {"arxiv": {"categories": ["CV"]}},
            "too fast": {"arxiv": {"request_delay": 1}},
            "bad max_results": {"max_results": "many"},
            "unbalanced quotes": {"keywords": {"A": {"filters": ['ti:"open']}}},
            "bad id": {"keywords": {"A": {"id": "Bad Id", "filters": ["a"]}}},
            "bad time limit": {"code_links": {"time_limit_minutes": 0}},
        }
        for label, patch in broken.items():
            with self.subTest(label):
                config = dict(base)
                config.update(patch)
                with self.assertRaises(da.ConfigError):
                    da.load_settings(self.write(yaml.safe_dump(config)))

    def test_missing_file(self):
        with self.assertRaises(da.ConfigError):
            da.load_settings(self.root / "missing.yaml")

    def test_repository_config(self):
        settings = da.load_settings(ROOT / "config.yaml")
        ids = [topic.id for topic in settings.topics]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(settings.topics), 10)
        self.assertGreaterEqual(settings.request_delay, 3.0)
        for topic in settings.topics:
            with self.subTest(topic=topic.id):
                self.assertTrue(topic.description)
                self.assertLess(len(topic.query), 2500)
                phrases, separator, categories = topic.query.rpartition(" AND (cat:")
                self.assertTrue(separator, "every topic is restricted to arXiv categories")
                self.assertNotIn("-", phrases, "hyphens must not reach the arXiv API")
                self.assertEqual(phrases.count('"') % 2, 0)
                self.assertEqual(phrases.count("("), phrases.count(")"))


    def test_site_settings_match_the_jekyll_configuration(self):
        settings = da.load_settings(ROOT / "config.yaml")
        jekyll = yaml.safe_load((ROOT / "docs" / "_config.yml").read_text(encoding="utf-8"))
        for key, expected in (("title", settings.title), ("description", settings.description),
                              ("repository", settings.repository)):
            with self.subTest(key=key):
                self.assertEqual(jekyll.get(key), expected,
                                 f"'{key}' in docs/_config.yml must match the 'site' section of config.yaml")


class FeedParsingTests(unittest.TestCase):
    def test_parses_entry(self):
        total, count, papers = da.parse_feed(atom_feed([SAMPLE_ENTRY], total=7))
        self.assertEqual((total, count, len(papers)), (7, 1, 1))
        paper = papers[0]
        self.assertEqual(paper["id"], "2611.01234")
        self.assertEqual(paper["title"], "Zero-Shot Composed Image Retrieval with & Diffusion Priors")
        self.assertEqual(paper["authors"], ["Alice Zhang", "Bob Müller"])
        self.assertEqual(paper["published"], "2026-11-16")
        self.assertEqual(paper["updated"], "2026-11-17")
        self.assertEqual(paper["primary"], "cs.CV")
        self.assertEqual(paper["categories"], ["cs.CV", "cs.IR"])  # ACM/MSC codes dropped
        self.assertEqual(paper["comment"], "Accepted to CVPR 2026; 10 pages")
        self.assertTrue(paper["abstract"].startswith("We study composed image retrieval (CIR). Code"))
        self.assertEqual(da.extract_code_url(paper["abstract"]), "https://github.com/example-lab/ZS-CIR")

    def test_legacy_identifier_and_missing_primary(self):
        xml = atom_entry("cs/0601001", "Old Paper", primary="bogus term", categories=["cs.IR"])
        _, _, papers = da.parse_feed(atom_feed([xml]))
        self.assertEqual(papers[0]["id"], "cs/0601001")
        self.assertEqual(papers[0]["primary"], "cs.IR")

    def test_api_error_entry(self):
        with self.assertRaises(da.ArxivQueryError):
            da.parse_feed(ERROR_FEED)

    def test_skips_incomplete_entries(self):
        broken = "<entry><id>http://arxiv.org/abs/2611.00001v1</id><title>No date</title></entry>"
        total, count, papers = da.parse_feed(atom_feed([broken, SAMPLE_ENTRY]))
        self.assertEqual((count, len(papers)), (2, 1))

    def test_empty_feed(self):
        self.assertEqual(da.parse_feed(atom_feed([], total=0)), (0, 0, []))


class ArxivClientTests(unittest.TestCase):
    def test_pagination_rate_limit_and_dedup(self):
        page1 = atom_feed([atom_entry("2611.00003", "C"), atom_entry("2611.00002", "B")], total=3)
        page2 = atom_feed([atom_entry("2611.00002", "B"), atom_entry("2611.00001", "A")], total=3, start=2)
        client, session, clock = make_client([FakeResponse(content=page1), FakeResponse(content=page2)], page_size=2)
        papers = client.search("q", 5)
        self.assertEqual([p["id"] for p in papers], ["2611.00003", "2611.00002", "2611.00001"])
        self.assertEqual([c["params"]["start"] for c in session.calls], [0, 2])
        self.assertEqual(session.calls[0]["params"]["sortBy"], "submittedDate")
        self.assertEqual(session.calls[0]["params"]["sortOrder"], "descending")
        self.assertEqual(clock.sleeps, [3.0])  # arXiv's required spacing between requests

    def test_respects_max_results(self):
        page = atom_feed([atom_entry(f"2611.0000{i}", f"T{i}") for i in range(3)], total=50)
        client, session, _ = make_client([FakeResponse(content=page)], page_size=100)
        self.assertEqual(len(client.search("q", 3)), 3)
        self.assertEqual(session.calls[0]["params"]["max_results"], 3)

    def test_retries_transient_errors(self):
        import requests
        ok = atom_feed([atom_entry("2611.00001", "A")])
        client, session, clock = make_client([
            FakeResponse(503, b"busy", {"Retry-After": "7"}),
            requests.ConnectionError("reset"),
            FakeResponse(200, b"<not-xml"),
            FakeResponse(200, ok),
        ])
        self.assertEqual(len(client.search("q", 10)), 1)
        self.assertEqual(len(session.calls), 4)
        self.assertIn(7.0, clock.sleeps)  # Retry-After honored

    def test_gives_up_after_max_retries(self):
        client, session, _ = make_client([FakeResponse(503)] * 3, max_retries=2)
        with self.assertRaises(da.ArxivError):
            client.search("q", 10)
        self.assertEqual(len(session.calls), 3)

    def test_query_errors_are_not_retried(self):
        # arXiv explains a rejected query in an Atom entry; a bare "400 Bad Request" means the same.
        for response in (FakeResponse(400, ERROR_FEED), FakeResponse(200, ERROR_FEED), FakeResponse(400, b"Bad Request")):
            with self.subTest(status=response.status_code, body=response.content[:12]):
                client, session, _ = make_client([response])
                with self.assertRaises(da.ArxivQueryError):
                    client.search("q", 10)
                self.assertEqual(len(session.calls), 1)

    def test_other_client_errors_are_not_query_errors(self):
        client, session, _ = make_client([FakeResponse(403, b"Forbidden")])
        with self.assertRaises(da.ArxivError) as caught:
            client.search("q", 10)
        self.assertNotIsInstance(caught.exception, da.ArxivQueryError)
        self.assertEqual(len(session.calls), 1)

    def test_empty_page_is_retried(self):
        full = atom_feed([atom_entry("2611.00001", "A")], total=1)
        client, session, _ = make_client([FakeResponse(content=atom_feed([], total=1)), FakeResponse(content=full)])
        self.assertEqual(len(client.search("q", 10)), 1)

    def test_keeps_earlier_pages_when_a_later_page_fails(self):
        page1 = atom_feed([atom_entry("2611.00002", "B")], total=2)
        client, _, _ = make_client([FakeResponse(content=page1)] + [FakeResponse(503)] * 2, page_size=1, max_retries=1)
        self.assertEqual([p["id"] for p in client.search("q", 5)], ["2611.00002"])

    def test_no_results(self):
        client, session, _ = make_client([FakeResponse(content=atom_feed([], total=0))])
        self.assertEqual(client.search("q", 10), [])
        self.assertEqual(len(session.calls), 1)


class HuggingFaceClientTests(unittest.TestCase):
    def make(self, responses: List[Any]) -> da.HuggingFaceClient:
        clock = FakeClock()
        return da.HuggingFaceClient(user_agent="test", session=FakeSession(responses), sleep=clock.sleep, clock=clock)

    def test_reads_github_repo(self):
        client = self.make([FakeResponse(payload={"githubRepo": "https://github.com/org/repo"}),
                            FakeResponse(404), FakeResponse(payload={"githubRepo": None})])
        self.assertEqual(client.code_url("2611.00001"), "https://github.com/org/repo")
        self.assertEqual(client.code_url("2611.00002"), "")
        self.assertEqual(client.code_url("2611.00003"), "")
        self.assertFalse(client.disabled)

    def test_disables_on_rate_limit_and_repeated_failures(self):
        client = self.make([FakeResponse(429)])
        client.code_url("x")
        self.assertTrue(client.disabled)
        self.assertEqual(client.code_url("y"), "")  # no further request
        client = self.make([FakeResponse(500)] * 5)
        for _ in range(5):
            client.code_url("x")
        self.assertTrue(client.disabled)


# ---------------------------------------------------------------------------
# Store and end-to-end tests
# ---------------------------------------------------------------------------

class StoreTests(TempProject):
    def test_upsert_merge_and_prune(self):
        store = da.PaperStore.load(self.settings.data_dir)
        self.assertEqual(store.upsert(entry("2611.00001", "A"), "composed", "2026-11-18"), (True, True))
        self.assertEqual(store.upsert(entry("2611.00001", "A v2", comment="CVPR"), "sketch-based-image-retrieval",
                                      "2026-11-19", code_url="https://github.com/a/b"), (False, True))
        record = store.papers["2611.00001"]
        self.assertEqual(record["title"], "A v2")
        self.assertEqual(record["added"], "2026-11-18")          # first-seen date is kept
        self.assertEqual(record["code"], "https://github.com/a/b")
        store.upsert(entry("2611.00001", "A v3"), "composed", "2026-11-20")
        self.assertEqual(record["code"], "https://github.com/a/b")  # existing link is kept
        self.assertNotIn("comment", record)                         # removed comment is dropped
        self.assertEqual(store.prune(["composed"], 0, NOW.date()), 0)
        self.assertEqual(record["topics"], ["composed"])
        self.assertEqual(store.prune(["other"], 0, NOW.date()), 1)
        self.assertEqual(store.papers, {})
        self.assertEqual(store.abstract("2611.00001"), "")

    def test_retention(self):
        store = da.PaperStore.load(self.settings.data_dir)
        store.upsert(entry("2401.00001", "Old", published="2024-01-02"), "composed", "2026-11-18")
        store.upsert(entry("2611.00001", "New", published="2026-11-10"), "composed", "2026-11-18")
        self.assertEqual(store.prune(["composed"], 365, NOW.date()), 1)
        self.assertEqual(list(store.papers), ["2611.00001"])

    def test_corrupt_database_is_not_overwritten(self):
        path = self.settings.data_dir / "papers.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json", encoding="utf-8")
        with self.assertRaises(da.StoreError):
            da.PaperStore.load(self.settings.data_dir)
        self.assertEqual(path.read_text(encoding="utf-8"), "{ not json")

    def test_json_lines_round_trip(self):
        payload = {"schema": 1, "generated": "x", "topics": [{"id": "a"}], "papers": [{"t": "é \"q\""}, {"t": 2}]}
        text = da.dumps_json_lines(payload)
        self.assertEqual(json.loads(text), payload)
        lines = text.splitlines()
        self.assertEqual(lines[0], "{")
        self.assertIn('{"t":"é \\"q\\""},', lines)  # one paper per line, UTF-8 kept
        self.assertIn('{"t":2}', lines)
        self.assertEqual(lines[-1], "}")
        self.assertEqual(json.loads(da.dumps_json_lines({"papers": []})), {"papers": []})


class EndToEndTests(TempProject):
    def fake_arxiv(self) -> FakeArxiv:
        return FakeArxiv({
            self.q_composed: [
                entry("2611.00002", "Composed <Retrieval> & {{ Liquid }}", published="2026-11-17",
                      abstract="Code: https://github.com/lab/cir."),
                entry("2611.00001", "Shared Paper", published="2026-11-16"),
            ],
            self.q_sketch: [entry("2611.00001", "Shared Paper", published="2026-11-16"),
                            entry("2610.09999", "Sketch Paper", published="2026-10-30")],
        })

    def run_update(self, arxiv: FakeArxiv, hf: Optional[FakeHF] = None, now: dt.datetime = NOW) -> int:
        return da.run_update(self.settings, client=arxiv, hf_client=hf or FakeHF(), now=now)

    def test_full_cycle(self):
        hf = FakeHF({"2610.09999": "https://github.com/sketch/sbir"})
        self.assertEqual(self.run_update(self.fake_arxiv(), hf), 0)
        db = self.database()
        self.assertEqual(db["schema"], 1)
        self.assertEqual(db["generated"], "2026-11-18T04:41:07Z")
        self.assertEqual([t["id"] for t in db["topics"]], ["composed", "sketch-based-image-retrieval"])
        papers = {p["id"]: p for p in db["papers"]}
        self.assertEqual(list(papers), ["2611.00002", "2611.00001", "2610.09999"])   # newest first
        self.assertEqual(papers["2611.00001"]["topics"], ["composed", "sketch-based-image-retrieval"])
        self.assertEqual(papers["2611.00002"]["code"], "https://github.com/lab/cir")  # from the abstract
        self.assertEqual(papers["2610.09999"]["code"], "https://github.com/sketch/sbir")  # via Hugging Face
        self.assertEqual(hf.asked, ["2611.00001", "2610.09999"])                      # only papers lacking code
        self.assertEqual(set(papers["2611.00001"]), {"id", "title", "authors", "published", "updated", "primary",
                                                     "categories", "topics", "added"})

        abstracts = json.loads((self.settings.data_dir / "abstracts/2611.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(abstracts), ["2611.00001", "2611.00002"])
        self.assertTrue((self.settings.data_dir / "abstracts/2610.json").exists())

        front = yaml.safe_load(self.settings.site_page.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(front["layout"], "papers")
        self.assertEqual(front["permalink"], "/")
        self.assertEqual(front["paper_count"], 3)
        self.assertEqual(front["last_updated"], "2026-11-18")

        topics = json.loads(self.settings.topics_data.read_text(encoding="utf-8"))
        self.assertEqual(topics[0]["filters"], ["composed image retrieval", 'ti:"text feedback"'])

        feed = ET.fromstring(self.settings.feed_path.read_bytes())
        ns = {"a": "http://www.w3.org/2005/Atom"}
        entries = feed.findall("a:entry", ns)
        self.assertEqual(len(entries), 3)
        self.assertEqual(feed.find("a:link[@rel='self']", ns).get("href"), "https://example.github.io/test-repo/feed.xml")
        first = entries[0]
        self.assertEqual(first.findtext("a:title", namespaces=ns), "Composed <Retrieval> & {{ Liquid }}")
        self.assertEqual(first.findtext("a:id", namespaces=ns), "https://arxiv.org/abs/2611.00002")
        self.assertTrue(first.findtext("a:summary", namespaces=ns))

    def test_second_identical_run_changes_nothing(self):
        self.run_update(self.fake_arxiv())
        files = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(self.run_update(self.fake_arxiv(), now=LATER), 0)
        self.assertEqual({p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}, files)
        self.assertEqual(self.database()["generated"], "2026-11-18T04:41:07Z")

    def test_new_paper_bumps_timestamp_and_keeps_history(self):
        self.run_update(self.fake_arxiv())
        arxiv = self.fake_arxiv()
        arxiv.results[self.q_composed].insert(0, entry("2611.00003", "Brand New", published="2026-11-18"))
        self.run_update(arxiv, now=LATER)
        db = self.database()
        self.assertEqual(db["generated"], "2026-11-19T04:40:00Z")
        added = {p["id"]: p["added"] for p in db["papers"]}
        self.assertEqual(added, {"2611.00003": "2026-11-19", "2611.00002": "2026-11-18",
                                 "2611.00001": "2026-11-18", "2610.09999": "2026-11-18"})

    def test_partial_and_total_failures(self):
        arxiv = self.fake_arxiv()
        arxiv.failing = {self.q_sketch}
        self.assertEqual(self.run_update(arxiv), 0)
        self.assertEqual(len(self.database()["papers"]), 2)
        arxiv.failing = {self.q_sketch, self.q_composed}
        before = (self.settings.data_dir / "papers.json").read_bytes()
        self.assertEqual(self.run_update(arxiv, now=LATER), 1)
        self.assertEqual((self.settings.data_dir / "papers.json").read_bytes(), before)

    def test_dry_run_lists_papers_and_writes_nothing(self):
        with self.assertLogs("arxiv_daily", level="INFO") as logs:
            self.assertEqual(da.run_update(self.settings, client=self.fake_arxiv(), hf_client=FakeHF(),
                                           dry_run=True, now=NOW), 0)
        self.assertFalse((self.root / "out").exists())
        listed = [line for line in logs.output if "2611.00001" in line]
        self.assertEqual(len(listed), 2)                     # once per topic that returned it
        self.assertIn("+ 2026-11-16  2611.00001", listed[0])  # "+" marks a paper new to the database
        self.assertIn("Shared Paper", listed[0])
        self.assertNotIn("+ 2026-11-16", listed[1])

    def test_regular_run_logs_only_new_papers(self):
        self.run_update(self.fake_arxiv())
        arxiv = self.fake_arxiv()
        arxiv.results[self.q_composed].insert(0, entry("2611.00003", "Brand New", published="2026-11-18"))
        with self.assertLogs("arxiv_daily", level="INFO") as logs:
            self.run_update(arxiv, now=LATER)
        papers = [line for line in logs.output if "  2611." in line or "  2610." in line]
        self.assertEqual(len(papers), 1)
        self.assertIn("Brand New", papers[0])

    def test_topic_without_results_is_reported(self):
        arxiv = self.fake_arxiv()
        arxiv.results[self.q_sketch] = []
        with self.assertLogs("arxiv_daily", level="WARNING") as logs:
            self.assertEqual(self.run_update(arxiv), 0)
        self.assertTrue(any("Sketch-Based Image Retrieval" in line and "matched no papers" in line
                            for line in logs.output))

    def test_non_ascii_description_and_bad_topic_lists(self):
        text = TEST_CONFIG.replace('description: "Test description."',
                                   'description: "Recherche d\'images – résumé ' + chr(0x1F50D) + '"')
        self.config_path.write_text(text, encoding="utf-8")
        settings = da.load_settings(self.config_path)
        da.run_update(settings, client=self.fake_arxiv(), hf_client=FakeHF(), now=NOW)
        front = yaml.safe_load(settings.site_page.read_text(encoding="utf-8").split("---")[1])
        self.assertTrue(front["description"].startswith(settings.description))
        # A hand-edited record with a malformed topic list is dropped instead of crashing.
        path = settings.data_dir / "papers.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["papers"][0]["topics"] = "composed"
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(da.run_rebuild(settings, now=LATER), 0)
        self.assertEqual(len(self.database()["papers"]), len(payload["papers"]) - 1)

    def test_huggingface_checks_the_newest_papers_first(self):
        arxiv = self.fake_arxiv()
        arxiv.results[self.q_composed].append(entry("2611.00005", "Listed Last", published="2026-11-18"))
        hf = FakeHF()
        self.run_update(arxiv, hf)
        self.assertEqual(hf.asked, ["2611.00005", "2611.00001", "2610.09999"])

    def test_huggingface_lookups_stop_at_the_time_limit(self):
        store = da.PaperStore.load(self.settings.data_dir)
        for number in range(1, 6):
            store.upsert(entry(f"2611.0000{number}", f"Paper {number}"), "composed", "2026-11-18")
        clock = FakeClock()

        class SlowHF(FakeHF):
            def code_url(self, paper_id: str) -> str:
                clock.sleep(40)                      # every lookup takes 40 seconds
                return super().code_url(paper_id)

        hf = SlowHF({"2611.00005": "https://github.com/a/b", "2611.00001": "https://github.com/c/d"})
        with self.assertLogs("arxiv_daily", level="WARNING") as logs:
            found = da.link_code_from_huggingface(store, hf, sorted(store.papers, reverse=True),
                                                  time_limit=100, clock=clock)
        self.assertEqual(hf.asked, ["2611.00005", "2611.00004", "2611.00003"])   # started at 0, 40 and 80 s
        self.assertEqual(found, 1)
        self.assertIn("2 papers were not checked", logs.output[0])
        self.assertNotIn("code", store.papers["2611.00001"])

    def test_link_refresh_and_rebuild(self):
        self.run_update(self.fake_arxiv())
        hf = FakeHF({"2611.00001": "https://github.com/late/release"})
        self.assertEqual(da.run_link_refresh(self.settings, hf_client=hf, now=LATER), 0)
        papers = {p["id"]: p for p in self.database()["papers"]}
        self.assertEqual(papers["2611.00001"]["code"], "https://github.com/late/release")
        self.assertNotIn("2611.00002", hf.asked)  # already linked
        self.settings.site_page.unlink()
        self.assertEqual(da.run_rebuild(self.settings, now=LATER), 0)
        self.assertTrue(self.settings.site_page.exists())

    def test_removed_topic_is_pruned_on_rebuild(self):
        self.run_update(self.fake_arxiv())
        text = TEST_CONFIG.split('  "Sketch-Based Image Retrieval":')[0]
        self.config_path.write_text(text, encoding="utf-8")
        settings = da.load_settings(self.config_path)
        da.run_rebuild(settings, now=LATER)
        db = self.database()
        self.assertEqual([p["id"] for p in db["papers"]], ["2611.00002", "2611.00001"])
        self.assertFalse((settings.data_dir / "abstracts/2610.json").exists())

    def test_empty_database_has_no_update_timestamp(self):
        self.assertEqual(da.run_rebuild(self.settings, now=NOW), 0)
        self.assertEqual(self.database()["generated"], "")
        self.assertEqual(self.database()["papers"], [])
        front = yaml.safe_load(self.settings.site_page.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual((front["last_updated"], front["paper_count"]), ("", 0))
        ET.fromstring(self.settings.feed_path.read_bytes())  # still a valid feed

    def test_command_line(self):
        self.assertEqual(da.main(["--config_path", str(self.config_path), "--rebuild"]), 0)
        self.assertEqual(da.main(["--config_path", str(self.config_path), "--topic", "nope", "--dry_run"]), 1)
        self.assertEqual(da.main(["--config_path", str(self.root / "missing.yaml"), "--rebuild"]), 1)
        rejected = (["--max_results", "0"], ["--rebuild", "--update_paper_links"],
                    ["--rebuild", "--topic", "composed"], ["--update_paper_links", "--max_results", "5"])
        for arguments in rejected:
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    da.main(["--config_path", str(self.config_path), *arguments])
                self.assertEqual(caught.exception.code, 2)


class OutageTests(TempProject):
    """Behaviour when arXiv is unreachable or refuses a query."""

    config_text = TEST_CONFIG + """  "Topic C":
    filters: ["topic c"]
  "Topic D":
    filters: ["topic d"]
"""

    def setUp(self) -> None:
        super().setUp()
        self.queries = [topic.query for topic in self.settings.topics]
        self.results = {query: [entry(f"2611.0000{n}", f"Paper {n}")] for n, query in enumerate(self.queries, 1)}

    def run_update(self, **faults: Any) -> Any:
        arxiv = FakeArxiv(self.results, **faults)
        code = da.run_update(self.settings, client=arxiv, hf_client=FakeHF(), now=NOW)
        return code, arxiv

    def test_stops_querying_after_consecutive_failures(self):
        code, arxiv = self.run_update(failing=set(self.queries[1:3]))
        self.assertEqual(code, 0)
        self.assertEqual(arxiv.queries, self.queries[:3])        # the fourth topic is not attempted
        self.assertEqual([p["id"] for p in self.database()["papers"]], ["2611.00001"])

    def test_single_failures_do_not_stop_the_run(self):
        code, arxiv = self.run_update(failing={self.queries[0], self.queries[2]})
        self.assertEqual(code, 0)
        self.assertEqual(arxiv.queries, self.queries)
        self.assertEqual(len(self.database()["papers"]), 2)

    def test_rejected_queries_do_not_count_as_an_outage(self):
        code, arxiv = self.run_update(rejected=set(self.queries[:2]))
        self.assertEqual(code, 0)
        self.assertEqual(arxiv.queries, self.queries)            # every topic is still tried
        self.assertEqual(len(self.database()["papers"]), 2)

    def test_total_outage_writes_nothing(self):
        code, arxiv = self.run_update(failing=set(self.queries))
        self.assertEqual(code, 1)
        self.assertEqual(arxiv.queries, self.queries[:2])
        self.assertFalse((self.root / "out").exists())


# ---------------------------------------------------------------------------
# Optional live check against the real arXiv API
# ---------------------------------------------------------------------------

def live_check() -> bool:
    """Ask the real arXiv API for the newest papers of every configured topic."""
    settings = da.load_settings(ROOT / "config.yaml")
    client = da.ArxivClient(user_agent=settings.user_agent, delay=settings.request_delay,
                            timeout=settings.timeout, max_retries=2)
    seconds = round((len(settings.topics) + 1) * (settings.request_delay + 1))
    print(f"\nLive check: asking arXiv for the 3 newest papers of each of the {len(settings.topics)} topics "
          f"(about {seconds} seconds)...")
    problems = 0
    for topic in settings.topics:
        try:
            papers = client.search(topic.query, 3)
        except da.ArxivQueryError as exc:
            problems += 1
            print(f"  REJECTED  {topic.name}: {exc}")
            continue
        except da.ArxivError as exc:
            print(f"  FAILED    {topic.name}: {exc}")
            print("Live check stopped: arXiv could not be reached. Check your connection and try again later.")
            return False
        if not papers:
            problems += 1
            print(f"  EMPTY     {topic.name}: no papers matched; check this topic's search phrases")
            continue
        newest = papers[0]
        print(f"  ok        {topic.name}: newest {newest['published']}  {newest['id']}  {newest['title'][:70]}")

    # Phrases are sent with spaces instead of hyphens (see daily_arxiv.sanitize_phrase).
    # Confirm that arXiv still finds titles that are written with a hyphen.
    probe = da.build_query(["sketch-based image retrieval"], ["ti"], ["cs.CV"])
    try:
        titles = [paper["title"] for paper in client.search(probe, 25)]
    except da.ArxivError as exc:
        problems += 1
        print(f"  FAILED    hyphen handling: {exc}")
    else:
        hyphenated = [title for title in titles if "sketch-based" in title.lower()]
        if hyphenated:
            print(f"  ok        hyphen handling: {len(hyphenated)} of {len(titles)} titles found for "
                  'the phrase "sketch based image retrieval" are written "sketch-based"')
        else:
            problems += 1
            print(f"  PROBLEM   hyphen handling: none of the {len(titles)} titles found for the phrase "
                  '"sketch based image retrieval" is written with a hyphen; hyphenated phrases may be missed. '
                  "Please report this.")
    print("Live check passed: every topic returned papers and hyphenated titles are found." if not problems
          else f"Live check found {problems} problem(s).")
    return not problems


class LiveCheckTests(unittest.TestCase):
    """The optional --live check itself, run offline with a stand-in for the arXiv client."""

    PROBE = da.build_query(["sketch-based image retrieval"], ["ti"], ["cs.CV"])

    def run_check(self, search: Callable[[str], List[Dict[str, Any]]]) -> Tuple[bool, str]:
        class Client:
            def __init__(self, **options: Any) -> None:
                pass

            def search(self, query: str, max_results: int) -> List[Dict[str, Any]]:
                return search(query)[:max_results]

        output = io.StringIO()
        with mock.patch.object(da, "ArxivClient", Client), contextlib.redirect_stdout(output):
            passed = live_check()
        return passed, output.getvalue()

    def test_passes_when_every_topic_and_the_hyphen_probe_return_papers(self):
        passed, text = self.run_check(lambda query: [entry("2611.00001", "Zero-Shot Sketch-Based Image Retrieval")])
        self.assertTrue(passed, text)
        self.assertEqual(text.count("  ok  "), len(da.load_settings(ROOT / "config.yaml").topics) + 1)
        self.assertIn("Live check passed", text)

    def test_reports_problems(self):
        first_query = da.load_settings(ROOT / "config.yaml").topics[0].query

        def unhyphenated(query: str) -> List[Dict[str, Any]]:
            return [entry("2611.00001", "Sketch Based Image Retrieval")]

        def empty_topic(query: str) -> List[Dict[str, Any]]:
            return [] if query == first_query else [entry("2611.00001", "Sketch-Based Image Retrieval")]

        def rejected_topic(query: str) -> List[Dict[str, Any]]:
            if query == first_query:
                raise da.ArxivQueryError("simulated syntax error")
            return [entry("2611.00001", "Sketch-Based Image Retrieval")]

        def outage(query: str) -> List[Dict[str, Any]]:
            raise da.ArxivError("simulated outage")

        for search, marker in ((unhyphenated, "PROBLEM   hyphen handling"), (empty_topic, "EMPTY "),
                               (rejected_topic, "REJECTED "), (outage, "Live check stopped")):
            with self.subTest(case=search.__name__):
                passed, text = self.run_check(search)
                self.assertFalse(passed, text)
                self.assertIn(marker, text)


if __name__ == "__main__":
    live = "--live" in sys.argv
    argv = [arg for arg in sys.argv if arg != "--live"]
    verbose = "-v" in argv or "--verbose" in argv
    for stream in (sys.stdout, sys.stderr):
        try:  # paper titles may not be encodable on every console
            stream.reconfigure(errors="backslashreplace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(format="[%(levelname)s] %(message)s", level=logging.INFO)
    if not verbose:  # the code under test logs a lot; show it only on request
        logging.getLogger("arxiv_daily").setLevel(logging.CRITICAL)
    result = unittest.main(argv=argv, exit=False, verbosity=2 if verbose else 1).result
    logging.getLogger("arxiv_daily").setLevel(logging.NOTSET)
    ok = result.wasSuccessful()
    if live and ok:
        ok = live_check()
    sys.exit(0 if ok else 1)
