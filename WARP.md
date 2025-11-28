# WARP.md

This file provides guidance to WARP (warp.dev) when working with code in this repository.

## Project overview

This repository contains a set of asynchronous Python scrapers built around `crawl4ai.AsyncWebCrawler` for crawling news sites via their sitemaps and saving content as Markdown plus lightweight JSON metadata.

Core targets (also documented in `readme.md`):
- `https://www.bhaskar.com/sitemaps-v1--sitemap-google-news-index.xml` (manual setup; not yet wired into a script here)
- `https://edition.cnn.com/sitemap/news.xml` and `https://edition.cnn.com/sitemap/article.xml` (CNN global / article sitemaps)
- `https://www.thelallantop.com/sitemaps/category-sitemap.xml` (Hindi; not yet wired into a script here)
- `https://www.aajtak.in/rssfeeds/news-sitemap.xml` (Hindi; actively used in the Aaj Tak scrapers)

There are no packaging or test configs in this repo (no `requirements.txt`, `pyproject.toml`, or test suite). All scripts are intended to be run directly with Python once dependencies are installed.

## Dependencies and environment

These scripts assume:
- Python 3.10+ (any reasonably recent Python 3 with `asyncio` and `typing` should work)
- Third‑party libraries (must be installed in your environment):
  - `crawl4ai`
  - `aiohttp` (used by `script2.py`)

Install them into your active environment (virtualenv, conda env, etc.):

```bash path=null start=null
pip install crawl4ai aiohttp
```

## Key commands

All commands below are run from the repository root.

### Quick single‑page crawl (Wired example)

Uses `script.py` to fetch a single article and save it as `output.md`.

- Run:

```bash path=null start=null
python script.py
```

This will:
- Use `AsyncWebCrawler` to crawl `https://www.wired.com/story/disinformation-minnesota-shooting-x/`
- Overwrite `output.md` with the article content in Markdown

To crawl a different URL, edit `OUTPUT_FILE` and the `url=` argument in `script.py`.

### Aaj Tak sitemap crawl (simple)

`script2.py` demonstrates a straightforward sitemap → pages flow for Aaj Tak:
- Downloads the sitemap XML via `aiohttp`
- Extracts all `<loc>` URLs
- Crawls each URL with `AsyncWebCrawler`
- Writes each page as Markdown into a flat `pages/` directory

- Run:

```bash path=null start=null
python script2.py
```

Notes:
- Output Markdown files are written under `pages/` with filenames derived from the URL (protocol stripped, `/` replaced with `_`).
- There is no retry, rate limiting, or progress persistence in this version.

### Aaj Tak sitemap crawl (robust, resumable)

`script3_aaj_tak.py` is the main production‑style crawler for Aaj Tak. It adds:
- Retry logic with exponential backoff (`retry_async`)
- Concurrency limiting via an `asyncio.Semaphore`
- Robust sitemap parsing that tolerates namespaces, tag variants, and CDATA
- Separate Markdown and JSON metadata outputs
- Progress tracking to resume runs
- Failure logging

- Run:

```bash path=null start=null
python script3_aaj_tak.py
```

This will:
- Fetch `SITEMAP_URL` (currently `https://www.aajtak.in/rssfeeds/news-sitemap.xml`) using `AsyncWebCrawler`
- Extract article URLs via `extract_urls_from_sitemap_robust`
- For each URL, write:
  - Markdown to `pages/<derived-name>.md`
  - JSON metadata (URL, title, language, HTTP status, timestamp) to `pages_meta/<derived-name>.json`
- Maintain a set of completed URLs in `pages_done.json` so subsequent runs only process new pages
- Log errors to `pages_failures.log`

To adjust crawling behavior, edit the configuration block at the top of `script3_aaj_tak.py` (sitemap URL, concurrency, retry settings, etc.).

### CNN Edition article crawl (by section)

`script_editon.py` orchestrates a multi‑stage crawl for CNN International (`edition.cnn.com`) from the article sitemap index.

High‑level flow:
1. Fetches the top‑level sitemap index (`SITEMAP_INDEX_URL`, default `https://edition.cnn.com/sitemap/article.xml`) via `AsyncWebCrawler`.
2. Extracts child sitemap URLs from the index (`extract_child_sitemaps`).
3. For each child sitemap (`process_child_sitemap`):
   - Fetches the sitemap XML.
   - Extracts CNN article URLs (`extract_article_urls_from_sitemap`), filtering to `https://www.cnn.com/YYYY/MM/DD/...`-style URLs.
   - Derives a `section` name from the sitemap URL path (e.g. `entertainment`, `world`).
   - Crawls each article, saving Markdown and metadata into section‑specific directories.

- Run the full CNN pipeline:

```bash path=null start=null
python script_editon.py
```

Outputs:
- Markdown content:
  - `edition_pages_by_section/<section>/<derived-name>.md`
- JSON metadata per article:
  - `edition_meta_by_section/<section>/<derived-name>.json`
  - Includes URL, title (from crawler result), language, HTTP status, and timestamp.
- Global progress file:
  - `edition_global_progress.json` — maps section → list of completed URLs so you can safely rerun the script.
- Failure log:
  - `edition_failures.log` — per‑URL errors encountered during sitemap or page processing.

Configuration knobs are defined at the top of `script_editon.py` (sitemap index URL, concurrency limits for sitemaps vs pages, retry settings, and base output directory paths).

## Code architecture

At a high level, the codebase consists of a few self‑contained scraper scripts that share common architectural patterns:

1. **Async orchestration layer (per script)**
   - Each major script (`script3_aaj_tak.py`, `script_editon.py`) defines a top‑level `main()` coroutine and an `if __name__ == "__main__": asyncio.run(main())` entrypoint.
   - Concurrency is controlled via module‑level `asyncio.Semaphore` instances (`SEM`, `SITEMAP_SEM`, `PAGE_SEM`) to limit simultaneous requests for sitemaps and article pages.
   - A shared `retry_async` helper wraps crawler calls with bounded retries and exponential backoff, so transient network errors do not abort the whole run.

2. **Robust sitemap parsing utilities**
   - Aaj Tak (`script3_aaj_tak.py`):
     - `extract_urls_from_sitemap_robust` attempts multiple strategies in order:
       1. Namespace‑aware `<url><loc>` parsing (`ns:url` / `ns:loc`).
       2. Non‑namespace `<url><loc>` parsing.
       3. Generic iteration over all elements whose tag ends with `loc`.
       4. Regex fallback that can extract URLs from plain `<loc>` tags as well as CDATA‑wrapped URLs.
     - A `debug_preview` helper prints the beginning of the sitemap to aid debugging when parsing fails.
   - CNN (`script_editon.py`):
     - `extract_child_sitemaps` parses the sitemap index to get child sitemap URLs, trying namespace‑aware XML parsing first and then a regex fallback over `<loc>` tags.
     - `extract_article_urls_from_sitemap` combines XML parsing and a strict `CNN_URL_REGEX` to filter only article‑shaped URLs, with additional normalization (e.g., upgrading `http://cnn.com/...` to `https://www.cnn.com/...`).

3. **URL → stable filename mapping and output layout**
   - Both robust scrapers share a `url_to_fname` helper:
     - Strips protocol from the URL and replaces `/` with `_` to create a readable base.
     - Appends a 12‑character SHA‑1 hash of the URL to avoid collisions and provide stable identifiers.
     - Optionally truncates the readable portion to keep filenames manageable.
   - Aaj Tak layout (`script3_aaj_tak.py`):
     - Markdown: `pages/<url_to_fname(url)>.md`
     - Metadata: `pages_meta/<url_to_fname(url)>.json`
   - CNN layout (`script_editon.py`):
     - Per‑section directories created via `ensure_section_dirs(section)`:
       - Markdown: `edition_pages_by_section/<section>/<url_to_fname(url)>.md`
       - Metadata: `edition_meta_by_section/<section>/<url_to_fname(url)>.json`

4. **Progress tracking and resumability**
   - Aaj Tak (`script3_aaj_tak.py`):
     - `pages_done.json` keeps a JSON list of URLs that have already been processed.
     - `load_progress` / `save_progress` operate on this set, using a temporary file + atomic `os.replace` to reduce corruption risk.
     - Before scraping a URL, `scrape_single_page` checks membership in the in‑memory `done_set` to skip already‑completed pages.
   - CNN (`script_editon.py`):
     - `edition_global_progress.json` is a JSON mapping `section -> [urls_done]`.
     - `load_progress` / `save_progress` manage this mapping, again via a temp file + atomic replace.
     - `process_child_sitemap` filters `article_urls` against the already‑done list for that section before scheduling new crawls.

5. **Error handling and logging**
   - Both robust scrapers:
     - Wrap critical operations in `try` / `except` blocks.
     - Use centralized `log_failure` helpers to append timestamped error lines to `pages_failures.log` or `edition_failures.log`.
     - Use `asyncio.gather(..., return_exceptions=True)` when launching many tasks so that a single failing task does not cancel the entire batch.

6. **Crawler abstraction
   - All scripts use `AsyncWebCrawler` from `crawl4ai` as the single abstraction for HTTP + parsing:
     - For sitemaps, they read `result.html` as plain XML text.
     - For article pages, they preferentially use `result.markdown` (falling back to `raw_markdown` / `fit_markdown` attributes when needed), and only then to `extracted_content` or `html`.
     - Metadata fields such as `title`, `language`, and `status_code` are plumbed through into per‑page JSON metadata files.

## Testing, linting, and formatting

This repository does not currently define any automated tests, linting configuration, or formatting tools. There are no `tests/` directories or tool configuration files (e.g., for `pytest`, `ruff`, or `black`).

If you add tests or tooling in the future, document the exact commands here (for example, how to run a subset of tests or perform a dry‑run crawl) so future Warp instances can use them directly.
