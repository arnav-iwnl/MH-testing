import asyncio
from crawl4ai import *

OUTPUT_FILE = "output.md"

async def main():
    async with AsyncWebCrawler() as crawler:
        result = await crawler.arun(
            url="https://www.wired.com/story/disinformation-minnesota-shooting-x/",
        )

        # Write (overwrite) the output file
        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            f.write(result.markdown)

        print(f"Saved output to {OUTPUT_FILE}")

if __name__ == "__main__":
    asyncio.run(main())
