---
layout: default
title: Setup Guide
nav_order: 3
description: "Deploy your own copy of Image Retrieval arXiv Daily, customize its topics and run it locally."
permalink: /setup/
---

# Setup Guide
{: .no_toc }

Run your own copy of the tracker, adapt the research topics, or work on it locally.

1. TOC
{:toc}

## Deploy your own copy

1. **Fork** the [repository](https://github.com/{{ site.repository }})
   (or push a copy to a new repository). In a fork, open the *Actions* tab once and
   enable workflows; GitHub keeps them switched off in new forks.
2. **Set your repository details.**
   - `config.yaml`: `site.url` → `https://<user>.github.io/<repository>/` and
     `site.repository` → `<user>/<repository>`.
   - `docs/_config.yml`: `author`, `repository`, `aux_links` and `footer_content`.
3. **Enable GitHub Pages.** *Settings → Pages → Build and deployment →* Source
   **Deploy from a branch**, Branch: your default branch, Folder: **/docs** → *Save*.
4. **Run the first update.** *Actions →* **Image Retrieval arXiv Daily** →
   *Run workflow*. For a deeper first backfill, enter `300` in the "Papers per
   topic" field.

The site appears at `https://<user>.github.io/<repository>/` about a minute after
the run. From then on, papers are updated every day at about 06:37 UTC and code
links are re-checked every Sunday.

## Customize the topics

Topics live under `keywords` in `config.yaml`:

```yaml
keywords:
  "Sketch-Based Image Retrieval":
    id: sketch-based-image-retrieval      # stable, URL-safe identifier
    description: "One sentence shown on the website."
    categories: [cs.CV, cs.GR]            # optional; overrides arxiv.categories
    filters:
      - "sketch-based image retrieval"    # exact phrase in title or abstract
      - 'ti:"sketch retrieval"'           # raw arXiv syntax: title only
```

- Plain phrases are matched as exact phrases in titles and abstracts, ignoring
  case. Hyphens are sent to arXiv as spaces (its API mishandles hyphenated terms),
  so `sketch-based` finds both "sketch-based" and "sketch based"; a one-word
  spelling such as `geolocalization` needs its own entry.
- Full phrases are more precise than short acronyms.
- Filters containing an arXiv field prefix (`ti:`, `abs:`, `au:`, `co:`, `jr:`,
  `cat:`, `rn:`, `all:`), parentheses or `AND` / `OR` / `ANDNOT` are passed to
  arXiv unchanged; write spaces instead of hyphens in them.
- Keep `id` stable. Removing a topic removes papers that belong to no other topic.
- See what a topic matches before committing:
  `python daily_arxiv.py --dry_run --topic sketch-based-image-retrieval`.

Other settings (papers per run, categories, code-link lookups, feed size and
retention) are documented in `config.yaml` and the
[README](https://github.com/{{ site.repository }}#configuration).

## Run locally

Requires Python 3.10 or newer.

```bash
python -m pip install -r requirements.txt
python test_local.py              # offline tests, no network needed
python test_local.py --live       # then query the real arXiv API once per topic
python daily_arxiv.py --dry_run   # list what every topic matches without writing files
python daily_arxiv.py             # fetch new papers and update docs/
```

| Option | Effect |
|---|---|
| `--max_results N` | Request N papers per topic (1–1000), e.g. for a backfill |
| `--topic ID` | Update only this topic (repeatable) |
| `--dry_run` | List the matching papers and write no files |
| `--update_paper_links` | Re-check code links of recent papers without one |
| `--rebuild` | Regenerate the site files from stored data, offline |
| `--config_path FILE` | Use another configuration file |

To preview the website (optional, needs Ruby with Bundler):

```bash
cd docs
bundle install
bundle exec jekyll serve          # then open the "Server address" it prints
```

## Troubleshooting

**The workflow cannot push.** The workflow requests write access itself. If your
account or organization restricts it, choose *Settings → Actions → General →
Workflow permissions →* **Read and write permissions**.

**The website shows "No papers yet".** Run the workflow manually once; scheduled
runs start the following day.

**The website does not change after a run.** Confirm that *Settings → Pages* uses
the `/docs` folder, and check the run log under *Actions*. Pages rebuilds about a
minute after each commit; a run that finds nothing new produces no commit.

**A topic returns unrelated papers.** Replace short or ambiguous phrases with longer
ones, or restrict the topic's `categories`.

**A topic "matched no papers".** Check its phrases with
`python daily_arxiv.py --dry_run --topic <id>` and add spelling variants.

**Scheduled runs stopped.** GitHub pauses scheduled workflows in repositories
without activity for 60 days; re-enable the workflow on the *Actions* tab.

Still stuck? [Open an issue](https://github.com/{{ site.repository }}/issues).
