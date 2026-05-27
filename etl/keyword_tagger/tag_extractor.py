import asyncio
import logging
import os
from pathlib import Path
from collections import Counter

from etl.loggers import setup_logging
from etl.scrapers.settings import PARSED_DIR
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)


async def extract_tags_from_file(filename: str):
    try:
        filepath = PARSED_DIR / filename
        data: dict = await read_json(filepath)

        if data["enrichment"]:
            if data["enrichment"]["tags"]:
                return data["enrichment"]["tags"]
        else:
            logger.error(f"Nie ma tagów dla pliku: {filename}")
        return []
    except Exception as e:
        logger.exception(f"Błąd: {e}")
        return []


async def main():
    filenames = os.listdir(PARSED_DIR)

    tasks = [extract_tags_from_file(filename) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tag_counts: Counter[str] = Counter()
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Błąd przy tagowaniu pliku: {res!r}")
        else:
            tag_counts.update(tag.strip() for tag in res if tag and tag.strip())

    await save_json(Path(__file__).resolve().parent / "tags.json", {"tags": dict(tag_counts)})


if __name__ == "__main__":
    asyncio.run(main())
