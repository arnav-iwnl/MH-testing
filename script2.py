import asyncio
import aiohttp
import xml.etree.ElementTree as ET
from crawl4ai import AsyncWebCrawler

SITEMAP_URL = "https://edition.cnn.com/sitemap/news.xml"   # <--- your sitemap URL
OUTPUT_DIR = "pages"

import os
os.makedirs(OUTPUT_DIR, exist_ok=True)


async def fetch_sitemap(url):
    """Download the sitemap XML."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url,ssl=False) as response:
            return await response.text()


def extract_urls(sitemap_xml):
    """Parse XML and extract all <loc> URLs."""
    root = ET.fromstring(sitemap_xml)
    urls = []
    ns = {"ns": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    for url_tag in root.findall("ns:url", ns):
        loc = url_tag.find("ns:loc", ns)
        if loc is not None:
            urls.append(loc.text)
    return urls


async def scrape_urls(urls):
    """Scrape each URL using crawl4ai."""
    async with AsyncWebCrawler() as crawler:
        tasks = []
        for url in urls:
            tasks.append(scrape_single_page(crawler, url))
        await asyncio.gather(*tasks)


async def scrape_single_page(crawler, url):
    """Scrape one page & save to file."""
    try:
        result = await crawler.arun(url=url)
        filename = url.replace("https://", "").replace("http://", "").replace("/", "_")
        filepath = f"{OUTPUT_DIR}/{filename}.md"

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(result.markdown)

        print(f"✔ Saved: {filepath}")

    except Exception as e:
        print(f"❌ Error scraping {url}: {e}")


async def main():
    sitemap_xml = await fetch_sitemap(SITEMAP_URL)
    urls = extract_urls(sitemap_xml)

    print(f"Found {len(urls)} URLs in sitemap.")
    await scrape_urls(urls)


if __name__ == "__main__":
    asyncio.run(main())
