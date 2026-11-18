<div align="center">

<img src="docs/assets/images/logo.svg" width="88" height="88" alt="Image Retrieval arXiv Daily logo">

# Image Retrieval arXiv Daily

**Daily-updated arXiv papers on image retrieval and visual search, organized by research topic.**

[![Daily update](https://github.com/Ezharjan/DailyImageretRievalArxiv/actions/workflows/arxiv-daily.yml/badge.svg)](https://github.com/Ezharjan/DailyImageretRievalArxiv/actions/workflows/arxiv-daily.yml)
[![Tests](https://github.com/Ezharjan/DailyImageretRievalArxiv/actions/workflows/tests.yml/badge.svg)](https://github.com/Ezharjan/DailyImageretRievalArxiv/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/github/license/Ezharjan/DailyImageretRievalArxiv)](LICENSE)

[Website](https://ezharjan.github.io/DailyImageretRievalArxiv/) ·
[Atom feed](https://ezharjan.github.io/DailyImageretRievalArxiv/feed.xml) ·
[JSON data](https://ezharjan.github.io/DailyImageretRievalArxiv/data/papers.json) ·
[Setup guide](https://ezharjan.github.io/DailyImageretRievalArxiv/setup/)

</div>

---

Image Retrieval arXiv Daily tracks new arXiv papers on image retrieval in computer
vision. A GitHub Actions workflow queries the arXiv API every day, sorts the
results into 13 research topics, links the authors' code repositories and
publishes a fast, searchable website on GitHub Pages.

## Contents

- [Features](#features)
- [Research topics](#research-topics)
- [How it works](#how-it-works)
- [Deploy your own copy](#deploy-your-own-copy)
- [Run locally](#run-locally)
- [Configuration](#configuration)
- [Automation](#automation)
- [Data files](#data-files)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License and acknowledgments](#license-and-acknowledgments)

## Features

**Collection**
- Precise arXiv queries: every search phrase is matched in titles and abstracts and
  restricted to relevant categories (cs.CV, cs.IR, cs.MM, ...).
- One database entry per paper, listed under every matching topic; revised versions
  refresh the stored metadata.
- Code links taken from abstracts and author comments, plus the Hugging Face Papers
  API; a weekly job re-checks recent papers.
- A dependency-light, polite arXiv client (rate limited to arXiv's terms of use,
  with timeouts, retries and backoff), covered by an offline test suite.

**Website**
- Renders in the browser from a compact JSON file, so the page stays fast no matter
  how many papers are collected (tested with more than 11,000 papers).
- Instant search over titles, authors, arXiv IDs, comments, journal references,
  categories and topic names; it ignores case and accents and accepts quoted phrases
  and arXiv URLs.
- Filters by topic, period (latest update, last 7/30/90 days, last 12 months) and
  code availability, with live counts; newest- or oldest-first sorting.
- Shareable URLs for every view, papers grouped by submission date, "New" badges for
  the latest update, abstracts on demand and one-click BibTeX.
- Responsive and accessible (keyboard shortcut <kbd>/</kbd>, screen-reader labels,
  reduced-motion support), plus an Atom feed for feed readers.

## Research topics

| Topic | Scope |
|---|---|
| Image Retrieval & Visual Search | The core topic: every paper that names image retrieval in its title or abstract, plus image search, similarity search, and near-duplicate or copy detection |
| Instance-Level & Landmark Retrieval | Landmark and particular-object retrieval, local and global descriptors, re-ranking, spatial verification |
| Image-Text & Cross-Modal Retrieval | Image-text matching, text-to-image and image-to-text retrieval, vision-language retrieval |
| Composed & Interactive Image Retrieval | Reference image plus modification text, zero-shot CIR, text feedback, dialog-based search |
| Sketch-Based Image Retrieval | Category-level, fine-grained and zero-shot SBIR, sketch-based 3D shape and video retrieval |
| Fine-Grained Retrieval & Metric Learning | Deep metric learning objectives and fine-grained image retrieval |
| Hashing & Efficient Retrieval | Deep and cross-modal hashing, product quantization, approximate nearest-neighbor search |
| Person & Object Re-Identification | Person, vehicle, animal and object re-ID, person search, text-based person retrieval |
| Visual Place Recognition & Geo-Localization | Visual place recognition, cross-view and image geo-localization |
| Video & Text-Video Retrieval | Text-to-video retrieval, video moment retrieval, content-based video retrieval, video copy detection |
| Multimodal Embeddings & Universal Retrieval | Universal multimodal retrieval, MLLM-based embedders, multimodal embedding models |
| Visual Document Retrieval & Multimodal RAG | Visual document retrieval, multimodal retrieval-augmented generation |
| Domain-Specific Image Retrieval | Remote sensing, medical and histopathology, fashion and products, art, trademarks, faces, food, 3D shapes |

The exact search phrases and categories of every topic are listed in
[`config.yaml`](config.yaml) and on the website's
[About page](https://ezharjan.github.io/DailyImageretRievalArxiv/about/).

## How it works

```
config.yaml           daily_arxiv.py              docs/  (GitHub Pages)
13 topics with  ───►  runs daily in        ───►   data/papers.json        ───►  papers page,
search phrases        GitHub Actions              data/abstracts/*.json         rendered in the
                                                  feed.xml, index.md            browser
```

1. **Query.** For each topic, `daily_arxiv.py` requests the newest papers (100 by
   default) whose title or abstract contains one of the topic's phrases, sorted by
   submission date.
2. **Merge.** Papers are merged into `docs/data/papers.json`. Existing papers keep
   the date they were first added; new topic matches are appended.
3. **Code links.** GitHub, GitLab, Codeberg and Bitbucket links (and anonymous
   review links) are read from abstracts and comments. Papers without one are
   looked up in the Hugging Face Papers API.
4. **Publish.** The workflow commits the changed files and GitHub Pages rebuilds
   the site. Files are only rewritten when their content changes, so a run that
   finds nothing new produces no commit.

## Deploy your own copy

1. **Fork** this repository (or push a copy to a new repository). In a fork, open
   the *Actions* tab once and enable workflows; GitHub keeps them switched off in
   new forks.
2. **Set your repository details**
   - `config.yaml`: `site.url` → `https://<user>.github.io/<repository>/` and
     `site.repository` → `<user>/<repository>`.
   - `docs/_config.yml`: `author`, `repository`, `aux_links` and `footer_content`.
3. **Enable GitHub Pages.** *Settings → Pages → Build and deployment →* Source
   **Deploy from a branch**, Branch: your default branch, Folder: **/docs** → *Save*.
4. **Run the first update.** *Actions →* **Image Retrieval arXiv Daily** → *Run
   workflow*. For a deeper first backfill, enter `300` in the "Papers per topic"
   field. The site is published at `https://<user>.github.io/<repository>/` about a
   minute after the run.

From then on the update runs automatically every day.

## Run locally

Requires Python 3.10 or newer.

```bash
git clone https://github.com/Ezharjan/DailyImageretRievalArxiv.git
cd DailyImageretRievalArxiv
python -m pip install -r requirements.txt

python test_local.py              # offline test suite (about a second, no network)
python test_local.py --live       # then query the real arXiv API once per topic (~1 minute)

python daily_arxiv.py --dry_run   # list what every topic matches without writing files
python daily_arxiv.py             # fetch new papers and update docs/
```

### Command-line reference

| Command | Effect |
|---|---|
| `python daily_arxiv.py` | Fetch new papers for every topic and regenerate the site data |
| `python daily_arxiv.py --max_results 300` | Request more papers per topic (1–1000), e.g. for a backfill |
| `python daily_arxiv.py --topic re-identification` | Update only the given topic id (repeatable) |
| `python daily_arxiv.py --dry_run` | Fetch and list the matching papers without writing any files |
| `python daily_arxiv.py --update_paper_links` | Re-check code links of recent papers that have none |
| `python daily_arxiv.py --rebuild` | Regenerate all site files from the stored data, without network access |
| `python daily_arxiv.py --config_path other.yaml` | Use another configuration file |
| `python daily_arxiv.py --verbose` | Show debug logging, including each query as arXiv interpreted it |

`--max_results` and `--topic` apply to fetching only; `--dry_run` can be combined
with every mode.

### Preview the website

The site is built by GitHub Pages; a local preview is optional and needs Ruby with
Bundler.

```bash
cd docs
bundle install
bundle exec jekyll serve          # then open the "Server address" it prints
```

## Configuration

All settings live in [`config.yaml`](config.yaml); every option is commented there.

| Setting | Default | Meaning |
|---|---|---|
| `max_results` | `100` | Newest papers requested per topic and run |
| `arxiv.categories` | cs.CV, cs.IR, cs.MM, cs.LG, cs.AI, cs.CL, cs.RO, cs.GR, eess.IV | A paper qualifies if any of its categories is listed (topics can set their own list) |
| `arxiv.search_fields` | `[ti, abs]` | Fields searched for plain phrases (title, abstract) |
| `arxiv.request_delay` | `3.0` | Seconds between API requests (arXiv requires at least 3) |
| `code_links.huggingface` | `true` | Ask the Hugging Face Papers API for missing code links |
| `code_links.refresh_days` | `90` | Age limit for `--update_paper_links` |
| `code_links.time_limit_minutes` | `30` | Stop the Hugging Face lookups of one run after this time (newest papers first) |
| `output.feed_size` | `100` | Papers in the Atom feed |
| `output.retention_days` | `0` | Remove papers older than this many days (0 keeps all) |

### Adding or editing a topic

```yaml
keywords:
  "Composed & Interactive Image Retrieval":
    id: composed-image-retrieval          # stable, URL-safe identifier
    description: "One sentence shown on the website."
    categories: [cs.CV, cs.IR]            # optional; overrides arxiv.categories
    filters:
      - "composed image retrieval"        # exact phrase in title or abstract
      - 'ti:"text feedback"'              # raw arXiv syntax: title only
```

- Plain phrases are matched as exact phrases in titles and abstracts, ignoring case.
  Hyphens are sent to arXiv as spaces (its API mishandles hyphenated terms), so
  `sketch-based` finds both "sketch-based" and "sketch based"; a one-word spelling
  such as `geolocalization` needs its own entry.
- Prefer full phrases over short acronyms, which are ambiguous.
- A filter with an arXiv field prefix (`ti:`, `abs:`, `au:`, `co:`, `jr:`, `cat:`,
  `rn:`, `all:`), parentheses or `AND`/`OR`/`ANDNOT` is passed to arXiv unchanged;
  write spaces instead of hyphens in such filters.
- Keep `id` stable: papers are stored with topic ids. Removing a topic removes papers
  that belong to no other topic at the next run.
- Check what a topic matches with `python daily_arxiv.py --dry_run --topic <id>`.

## Automation

[`.github/workflows/arxiv-daily.yml`](.github/workflows/arxiv-daily.yml) runs:

| Schedule (UTC) | Task |
|---|---|
| Daily, 06:37 | Fetch new papers |
| Sundays, 07:47 | Re-check code links of papers from the last 90 days |

arXiv announces new submissions at 20:00 US Eastern time, Sunday to Thursday, and its
API lists them from the following midnight (04:00 or 05:00 UTC); the daily run starts
after that all year round.

GitHub may start scheduled runs some minutes late. The workflow can also be started
manually (*Actions → Image Retrieval arXiv Daily → Run workflow*) with the task
`update`, `refresh-code-links` or `rebuild`. Both tasks share one concurrency
group, check out the latest commit, run the offline tests first and commit as
`github-actions[bot]` only when data changed.
[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs the tests on
Python 3.10, 3.12 and 3.13 for every change to the code or configuration.

## Data files

All generated files live in `docs/` and are rewritten by `daily_arxiv.py`; do not
edit them by hand.

| File | Content |
|---|---|
| `docs/data/papers.json` | Topics and paper metadata (one paper per line) |
| `docs/data/abstracts/YYMM.json` | Abstracts, split by the arXiv month in the ID |
| `docs/_data/topics.json` | Topic definitions for the About page |
| `docs/index.md` | Front matter of the papers page (update date, counts) |
| `docs/feed.xml` | Atom feed of the most recently added papers |

A record in `papers.json`:

```json
{"id":"2611.01234","title":"…","authors":["…"],"published":"2026-11-16","updated":"2026-11-17",
 "primary":"cs.CV","categories":["cs.CV","cs.IR"],"comment":"Accepted to CVPR 2026",
 "code":"https://github.com/…","topics":["composed-image-retrieval"],"added":"2026-11-18"}
```

`published` is the submission date of the first version (UTC), `updated` the date of
the latest version and `added` the date the paper entered this collection. The
optional fields `comment`, `journal`, `doi` and `code` are omitted when empty.

## Troubleshooting

| Problem | Solution |
|---|---|
| The workflow cannot push ("Permission denied", HTTP 403) | The workflow requests write access itself; if your account or organization restricts it, choose *Settings → Actions → General → Workflow permissions →* **Read and write permissions** |
| The website shows "No papers yet" | Run the workflow once manually; scheduled runs start the next day |
| The website does not update | Check *Settings → Pages* (source `/docs`) and the latest run under *Actions*; Pages rebuilds a minute after each commit |
| A topic returns unrelated papers | Replace short or ambiguous phrases with longer ones, or restrict the topic's `categories` |
| A topic finds too few papers, or the log says it "matched no papers" | Add synonyms and spelling variants, then check with `--dry_run --topic <id>`; increase `max_results` for a longer history |
| "arXiv API returned HTTP 503" or "HTTP 429" | arXiv is busy; the client retries automatically and the next run catches up |
| Scheduled runs stopped | GitHub pauses schedules in repositories without activity for 60 days; re-enable the workflow under *Actions* |

## Contributing

Suggestions for topics and search phrases, bug reports and pull requests are welcome.

1. Edit `config.yaml` or the code.
2. Run `python test_local.py` and, for topic changes,
   `python daily_arxiv.py --dry_run --topic <id>`.
3. Open a pull request; the template lists the checks.

Report problems on the [issue tracker](https://github.com/Ezharjan/DailyImageretRievalArxiv/issues).

## License and acknowledgments

Released under the [MIT License](LICENSE).

- Paper metadata: [arXiv API](https://info.arxiv.org/help/api/index.html). Thank you to
  arXiv for use of its open access interoperability. This project is independent and
  not affiliated with or endorsed by arXiv.
- Code links: author-provided links in arXiv abstracts and the
  [Hugging Face Papers](https://huggingface.co/papers) API.
- Website: [Jekyll](https://jekyllrb.com/), [Just the Docs](https://just-the-docs.com/)
  and GitHub Pages.

Maintainer: [@Ezharjan](https://github.com/Ezharjan) · <mysoft@111.com>
