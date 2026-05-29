import asyncio
import logging
from pathlib import Path
from collections import Counter
from etl.settings import INDUSTRIES_PATH
from etl.loggers import setup_logging
from etl.scrapers.settings import PARSED_DIR
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)


async def read_offer_industry(filename: str):
    try:
        filepath = PARSED_DIR / filename
        data: dict = await read_json(filepath)

        enrichment = data.get("enrichment", {})
        industry = enrichment.get("industry", [])
        if industry:
            return industry
        else:
            logger.error(f"Nie ma tagów dla pliku: {filename}")
        return []
    except Exception as e:
        logger.exception(f"Błąd: {e}")
        return []


async def main():
    filenames = [f.name for f in PARSED_DIR.iterdir() if f.is_file() and f.suffix == ".json"]

    tasks = [read_offer_industry(filename) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    industries: dict[str, int] = {}
    for res in results:
        if isinstance(res, Exception):
            logger.error(f"Błąd przy czytaniu pliku: {res!r}")
        else:
            if res in industries:
                industries[res] += 1
            else:
                industries[res] = 1

    industries = {key.replace("_", " "): industries.pop(key) for key in list(industries.keys())}

    await save_json(INDUSTRIES_PATH, {"industries": industries})


if __name__ == "__main__":
    asyncio.run(main())
