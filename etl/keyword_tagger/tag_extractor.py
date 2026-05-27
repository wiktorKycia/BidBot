import asyncio
import logging
import os
from pathlib import Path

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
    filenames = os.listdir(PARSED_DIR)

    tasks = [extract_tags_from_file(filename) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tag_list: set[str] = set()
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Błąd przy tagowaniu pliku: {res!r}")
        else:
            tag_list.update(res)

    await save_json(Path(__file__).resolve().parent / "tags.json", {"tags": [tag.strip() for tag in tag_list]})


if __name__ == "__main__":
    asyncio.run(main())
