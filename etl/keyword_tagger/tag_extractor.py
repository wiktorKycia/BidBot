import asyncio
import logging
from pathlib import Path
from collections import Counter
from etl.settings import TAGS_PATH
from etl.loggers import setup_logging
from etl.scrapers.settings import PARSED_DIR
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)


async def extract_tags_from_file(filename: str):
    try:
        filepath = PARSED_DIR / filename
        data: dict = await read_json(filepath)

        enrichment = data.get("enrichment", {})
        tags = enrichment.get("tags", [])
        if tags:
            return tags
        else:
            logger.error(f"Nie ma tagów dla pliku: {filename}")
        return []
    except Exception as e:
        logger.exception(f"Błąd: {e}")
        return []


async def main():
    filenames = [f.name for f in PARSED_DIR.iterdir() if f.is_file() and f.suffix == ".json"]

    tasks = [extract_tags_from_file(filename) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tag_counts: Counter[str] = Counter()
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Błąd przy tagowaniu pliku: {res!r}")
        else:
            tag_counts.update(tag.strip() for tag in res if tag and tag.strip())

    await save_json(TAGS_PATH, {"tags": dict(tag_counts)})


if __name__ == "__main__":
    asyncio.run(main())
