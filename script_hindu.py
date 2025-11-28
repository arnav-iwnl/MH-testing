#!/usr/bin/env python3
"""
script_hindu.py

Crawl The Hindu RSS feeds for India (national) and World news and save each
article as Markdown plus JSON metadata, with per-section directories.

Feeds:
- India (national):      https://www.thehindu.com/news/national/feeder/default.rss
- World (international): https://www.thehindu.com/news/international/feeder/default.rss
"""

import asyncio
import json
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from hashlib import sha1
from typing import Dict, List

from crawl4ai import AsyncWebCrawler

# ---------- CONFIG ----------
FEEDS: Dict[str, str] = {
    "india": "https://www.thehindu.com/news/national/feeder/default.rss",
    "world": "https://www.thehindu.com/news/international/feeder/default.rss",
}

BASE_OUTPUT_DIR = "hindu_pages_by_section"   # per-section markdown
BASE_META_DIR = "hindu_meta_by_section"      # per-section metadata
LOG_FILE = "hindu_failures.log"
PROGRESS_FILE = "hindu_progress.json"       # section -> list of urls

CONCURRENCY_PAGES = 6
MAX_RETRIES = 3
BASE_BACKOFF = 1.0
FEED_PREVIEW_CHARS = 800

# ensure base dirs exist
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(BASE_META_DIR, exist_ok=True)

PAGE_SEM = asyncio.Semaphore(CONCURRENCY_PAGES)


# ---------- helpers ----------
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def url_to_fname(url: str) -> str:
    """Create a filesystem-friendly, mostly-stable filename from a URL."""
    h = sha1(url.encode("utf-8")).hexdigest()[:12]
    nice = url.replace("https://", "").replace("http://", "").replace("/", "_")
    nice = (nice[:60] + "...") if len(nice) > 60 else nice
    return f"{nice}_{h}"


def ensure_section_dirs(section: str):
    out_dir = os.path.join(BASE_OUTPUT_DIR, section)
    meta_dir = os.path.join(BASE_META_DIR, section)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(meta_dir, exist_ok=True)
    return out_dir, meta_dir


def load_progress() -> Dict[str, List[str]]:
    if os.path.exists(PROGRESS_FILE):
        try:
            with open(PROGRESS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    # normalise values to lists of strings
                    return {k: list(v) for k, v in data.items()}
        except Exception:
            return {}
    return {}


def save_progress(progress: Dict[str, List[str]]):
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)


def log_failure(target: str, error: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {target}  |  {error}\n")


async def retry_async(fn, *args, max_retries=MAX_RETRIES, base_backoff=BASE_BACKOFF, **kwargs):
    attempt = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:  # noqa: BLE001
            attempt += 1
            if attempt >= max_retries:
                raise
            backoff = base_backoff * (2 ** (attempt - 1))
            await asyncio.sleep(backoff)


def debug_preview(text: str, n: int = FEED_PREVIEW_CHARS):
    """Print a short preview of feed XML for debugging malformed feeds."""
    print(f"(feed length = {len(text)} bytes)")
    print("=== FEED PREVIEW ===")
    print(text[:n])
    print("====================")


def extract_article_urls_from_feed(xml_text: str) -> List[str]:
    """Robust extraction of article links from an RSS feed.

    Matches The Hindu format, e.g.:

        <item>
          <link><![CDATA[ https://www.thehindu.com/news/national/... ]]></link>
        </item>

    Strategy:
    1) Try XML parsing; for each <item>, find its <link>.
    2) Fallback regex: pull out https URLs and filter to thehindu.com.
    """
    urls: List[str] = []

    if not xml_text or len(xml_text) < 20:
        return urls

    # 1) XML parse and walk items/links, ignoring namespaces
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter():
            tag = item.tag or ""
            if tag.endswith("item"):
                for child in item:
                    ctag = child.tag or ""
                    if ctag.endswith("link") and child.text:
                        link = child.text.strip()
                        if link.startswith("http") and "thehindu.com" in link:
                            urls.append(link)
        if urls:
            seen = set()
            out: List[str] = []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            return out
    except Exception:
        # fall through to regex fallback
        debug_preview(xml_text, n=FEED_PREVIEW_CHARS)

    # 2) regex fallback: any thehindu.com URL in the feed
    regex = re.compile(r"https?://[^\s<]+thehindu\.com[^\s<]*", re.IGNORECASE)
    found = regex.findall(xml_text)
    seen = set()
    out: List[str] = []
    for u in found:
        u = u.strip()
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def fetch_text_via_crawler(url: str, crawler: AsyncWebCrawler) -> str:
    res = await retry_async(crawler.arun, url=url)
    text = getattr(res, "html", "") or ""
    if not text:
        raise RuntimeError(f"Empty response from {url}")
    return text


# ---------- scraping single article ----------
async def scrape_article_and_save(
    crawler: AsyncWebCrawler,
    url: str,
    section: str,
    progress: Dict[str, List[str]],
):
    """Crawl one article URL and save markdown + metadata under the section."""
    done_list = progress.get(section, [])
    if url in done_list:
        return

    async with PAGE_SEM:
        try:
            result = await retry_async(crawler.arun, url=url)

            # Extract markdown (string or object) or fall back to html/extracted_content
            md = ""
            if getattr(result, "markdown", None):
                md_field = result.markdown
                if isinstance(md_field, str):
                    md = md_field
                else:
                    md = (
                        getattr(md_field, "raw_markdown", None)
                        or getattr(md_field, "fit_markdown", None)
                        or ""
                    )
            if not md:
                md = (
                    getattr(result, "extracted_content", None)
                    or getattr(result, "html", "")
                    or ""
                )

            out_dir, meta_dir = ensure_section_dirs(section)
            fname = url_to_fname(url)
            md_path = os.path.join(out_dir, f"{fname}.md")
            meta_path = os.path.join(meta_dir, f"{fname}.json")

            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md or "")

            metadata = {
                "url": url,
                "title": getattr(result, "title", None)
                or (getattr(result, "metadata", {}) or {}).get("title"),
                "lang": getattr(result, "language", None),
                "status_code": getattr(result, "status_code", None),
                "timestamp": now_iso(),
                "section": section,
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            print(f"✔ Saved [{section}]: {md_path}")

            # mark done in progress structure
            progress.setdefault(section, [])
            progress[section].append(url)
            save_progress(progress)

        except Exception as e:  # noqa: BLE001
            print(f"❌ Error scraping {url}: {e}")
            log_failure(url, str(e))


# ---------- feed orchestrator ----------
async def process_feed(
    crawler: AsyncWebCrawler,
    section: str,
    feed_url: str,
    progress: Dict[str, List[str]],
):
    try:
        print(f"Fetching feed for section '{section}': {feed_url}")
        feed_xml = await fetch_text_via_crawler(feed_url, crawler)
        urls = extract_article_urls_from_feed(feed_xml)
        print(f"  -> extracted {len(urls)} article URLs from feed '{section}'")

        done_urls = set(progress.get(section, []))
        to_crawl = [u for u in urls if u not in done_urls]
        print(f"  -> will crawl {len(to_crawl)} new articles for section '{section}'")

        tasks = [
            scrape_article_and_save(crawler, u, section, progress) for u in to_crawl
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for u, r in zip(to_crawl, results):
            if isinstance(r, Exception):
                log_failure(u, f"Task failed: {r}")
    except Exception as e:  # noqa: BLE001
        print(f"❌ Failed to process feed {feed_url} for section '{section}': {e}")
        log_failure(feed_url, str(e))


# ---------- main ----------
async def main():
    progress = load_progress()  # section -> list(urls done)

    async with AsyncWebCrawler() as crawler:
        tasks = [
            process_feed(crawler, section, url, progress)
            for section, url in FEEDS.items()
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    print("\n🎉 DONE — all feeds processed (or attempted).")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
