#!/usr/bin/env python3
"""
crawl_cnn_index.py

Flow:
1) Fetch top-level sitemap index (e.g. https://edition.cnn.com/sitemap/article.xml) using AsyncWebCrawler
2) Extract child sitemap URLs
3) For each child sitemap:
     - fetch sitemap XML
     - extract CNN article endpoints like https://www.cnn.com/YYYY/MM/DD/slug
     - crawl each article and save markdown + metadata into section-specific directory
"""

import asyncio
import os
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from hashlib import sha1
from typing import List, Set, Dict
from crawl4ai import AsyncWebCrawler

# ---------- CONFIG ----------
SITEMAP_INDEX_URL = "https://edition.cnn.com/sitemap/article.xml"

BASE_OUTPUT_DIR = "edition_pages_by_section"   # will contain subfolders per section
BASE_META_DIR = "edition_meta_by_section"
LOG_FILE = "edition_failures.log"
PROGRESS_FILE = "edition_global_progress.json"  # global progress mapping section -> list of urls

CONCURRENCY_SITEMAPS = 4   # how many child sitemaps to fetch in parallel
CONCURRENCY_PAGES = 6      # how many article pages to fetch in parallel per crawler instance
MAX_RETRIES = 3
BASE_BACKOFF = 1.0
SITEMAP_PREVIEW_CHARS = 600

# ensure base dirs exist
os.makedirs(BASE_OUTPUT_DIR, exist_ok=True)
os.makedirs(BASE_META_DIR, exist_ok=True)

# semaphores
SITEMAP_SEM = asyncio.Semaphore(CONCURRENCY_SITEMAPS)
PAGE_SEM = asyncio.Semaphore(CONCURRENCY_PAGES)

# ---------- helpers ----------
def now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"

def url_to_fname(url: str) -> str:
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
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_progress(progress: Dict[str, List[str]]):
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)
    os.replace(tmp, PROGRESS_FILE)

def log_failure(url: str, error: str):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}] {url}  |  {error}\n")

async def retry_async(fn, *args, max_retries=MAX_RETRIES, base_backoff=BASE_BACKOFF, **kwargs):
    attempt = 0
    while True:
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            attempt += 1
            if attempt >= max_retries:
                raise
            backoff = base_backoff * (2 ** (attempt - 1))
            await asyncio.sleep(backoff)

# ---------- sitemap index parsing ----------
def debug_preview(text: str, n: int = SITEMAP_PREVIEW_CHARS):
    print(f"(sitemap length = {len(text)} bytes)")
    print("=== SITEMAP PREVIEW ===")
    print(text[:n])
    print("=======================")

def extract_child_sitemaps(index_xml: str) -> List[str]:
    """
    Extract <sitemap><loc>...</loc></sitemap> links from a sitemap index.
    Robust: namespace-aware then fallback to regex.
    """
    sitemaps = []
    if not index_xml or len(index_xml) < 20:
        return sitemaps

    debug_preview(index_xml, n=SITEMAP_PREVIEW_CHARS)

    # 1) namespace-aware
    try:
        root = ET.fromstring(index_xml)
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        found = root.findall("ns:sitemap", ns)
        if found:
            for s in found:
                loc = s.find("ns:loc", ns)
                if loc is not None and loc.text:
                    sitemaps.append(loc.text.strip())
            if sitemaps:
                return sitemaps
    except Exception:
        pass

    # 2) fallback regex for <loc> inside sitemapindex
    regex = re.compile(r"<loc>\s*(?:<!\[CDATA\[\s*)?(https?://[^<\]\s]+)(?:\s*\]\]>)?\s*</loc>", re.IGNORECASE)
    found = regex.findall(index_xml)
    # dedupe preserve order
    seen = set(); out=[]
    for u in found:
        if u not in seen:
            seen.add(u); out.append(u)
    return out

# ---------- child sitemap -> article urls ----------
CNN_URL_REGEX = re.compile(
    r"https?://(?:www\.)?cnn\.com/\d{4}/\d{2}/\d{2}/[A-Za-z0-9\-\_~/]+",
    re.IGNORECASE,
)

def extract_article_urls_from_sitemap(xml_text: str) -> List[str]:
    """
    Robustly extract article URLs from a child sitemap XML or raw text.
    """
    urls = []
    if not xml_text or len(xml_text) < 20:
        return urls

    # try namespace-aware first
    try:
        root = ET.fromstring(xml_text)
        ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        found = root.findall("ns:url", ns)
        if found:
            for item in found:
                loc = item.find("ns:loc", ns)
                if loc is not None and loc.text:
                    urls.append(loc.text.strip())
            # filter to cnn article pattern
            if urls:
                filtered = [u for u in urls if CNN_URL_REGEX.search(u)]
                # dedupe preserve order
                seen = set(); out=[]
                for u in filtered:
                    if u not in seen:
                        seen.add(u); out.append(u)
                return out
    except Exception:
        pass

    # generic iteration for tags ending with 'loc'
    try:
        root = ET.fromstring(xml_text)
        for el in root.iter():
            if el.tag and el.tag.endswith("loc") and el.text:
                urls.append(el.text.strip())
        filtered = [u for u in urls if CNN_URL_REGEX.search(u)]
        seen = set(); out=[]
        for u in filtered:
            if u not in seen:
                seen.add(u); out.append(u)
        if out:
            return out
    except Exception:
        pass

    # regex fallback: find CNN article-like URLs in raw text
    found = CNN_URL_REGEX.findall(xml_text)
    seen=set(); out=[]
    for u in found:
        normalized = u
        if normalized.startswith("http://"):
            normalized = "https://" + normalized[len("http://"):]
        if "://cnn.com/" in normalized and "://www." not in normalized:
            normalized = normalized.replace("://cnn.com/", "://www.cnn.com/")
        if normalized not in seen:
            seen.add(normalized); out.append(normalized)
    return out

# ---------- fetch helpers (use AsyncWebCrawler) ----------
async def fetch_text_via_crawler(url: str, crawler: AsyncWebCrawler) -> str:
    res = await retry_async(crawler.arun, url=url)
    text = getattr(res, "html", "") or ""
    if not text:
        raise RuntimeError(f"Empty response from {url}")
    return text

# ---------- scraping single article ----------
async def scrape_article_and_save(crawler: AsyncWebCrawler, url: str, section: str, progress: Dict[str, List[str]]):
    """
    Crawl one article and save markdown + metadata to section-specific dirs.
    progress is a dict mapping section->list_of_completed_urls (will be saved by caller)
    """
    done_list = progress.get(section, [])
    if url in done_list:
        return

    async with PAGE_SEM:
        try:
            result = await retry_async(crawler.arun, url=url)

            # extract markdown (string or object) or fallback to html/extracted_content
            md = ""
            if getattr(result, "markdown", None):
                md_field = result.markdown
                if isinstance(md_field, str):
                    md = md_field
                else:
                    md = getattr(md_field, "raw_markdown", None) or getattr(md_field, "fit_markdown", None) or ""
            if not md:
                md = getattr(result, "extracted_content", None) or getattr(result, "html", "") or ""

            out_dir, meta_dir = ensure_section_dirs(section)
            fname = url_to_fname(url)
            md_path = os.path.join(out_dir, f"{fname}.md")
            meta_path = os.path.join(meta_dir, f"{fname}.json")

            with open(md_path, "w", encoding="utf-8") as f:
                f.write(md or "")

            metadata = {
                "url": url,
                "title": getattr(result, "title", None) or (getattr(result, "metadata", {}) or {}).get("title"),
                "lang": getattr(result, "language", None),
                "status_code": getattr(result, "status_code", None),
                "timestamp": now_iso(),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(metadata, f, indent=2)

            print(f"✔ Saved [{section}]: {md_path}")

            # mark done in progress structure
            progress.setdefault(section, [])
            progress[section].append(url)
            # atomic save after each successful article (safe if many)
            save_progress(progress)

        except Exception as e:
            print(f"❌ Error scraping {url}: {e}")
            log_failure(url, str(e))

# ---------- top-level orchestrator ----------
async def process_child_sitemap(crawler: AsyncWebCrawler, sitemap_url: str, progress: Dict[str, List[str]]):
    """
    Fetch a child sitemap, extract article URLs, and crawl them.
    We'll derive a section name from the sitemap url path, e.g.
    https://www.cnn.com/sitemap/article/entertainment/2025/11.xml -> "entertainment"
    """
    async with SITEMAP_SEM:
        try:
            print(f"Fetching child sitemap: {sitemap_url}")
            sitemap_text = await fetch_text_via_crawler(sitemap_url, crawler)
            article_urls = extract_article_urls_from_sitemap(sitemap_text)
            print(f"  -> extracted {len(article_urls)} article URLs from {sitemap_url}")

            # derive section
            # try to capture the second-last or third path segment: .../article/{section}/{year}/{month}.xml
            section = "unknown"
            try:
                parts = sitemap_url.split("/")
                # find index of 'article' then next element is section
                if "article" in parts:
                    idx = parts.index("article")
                    if idx + 1 < len(parts):
                        section = parts[idx + 1]
                # fallback: last meaningful directory name
                if not section:
                    section = parts[-2] if len(parts) >= 2 else "unknown"
            except Exception:
                section = "unknown"

            # ensure directory for section exists
            ensure_section_dirs(section)

            # filter out already-done urls
            done_urls = set(progress.get(section, []))
            to_crawl = [u for u in article_urls if u not in done_urls]
            print(f"  -> will crawl {len(to_crawl)} new articles for section '{section}'")

            # crawl articles (reuse same crawler)
            tasks = [scrape_article_and_save(crawler, u, section, progress) for u in to_crawl]
            # use gather with return_exceptions so one failure won't cancel others
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for u, r in zip(to_crawl, results):
                if isinstance(r, Exception):
                    log_failure(u, f"Task failed: {r}")

        except Exception as e:
            print(f"❌ Failed to process sitemap {sitemap_url}: {e}")
            log_failure(sitemap_url, str(e))

async def main():
    progress = load_progress()   # dict section -> list(urls done)

    async with AsyncWebCrawler() as crawler:
        print("Fetching top-level sitemap index via AsyncWebCrawler...")
        index_text = await fetch_text_via_crawler(SITEMAP_INDEX_URL, crawler)

        child_sitemaps = extract_child_sitemaps(index_text)
        print(f"Found {len(child_sitemaps)} child sitemaps in index.")

        # optional: filter or sort child sitemaps (e.g., newest first). We'll process all.
        # Process child sitemaps with limited concurrency
        tasks = [process_child_sitemap(crawler, s, progress) for s in child_sitemaps]
        await asyncio.gather(*tasks, return_exceptions=True)

    print("\n🎉 DONE — all child sitemaps processed (or attempted).")

if __name__ == "__main__":
    asyncio.run(main())
