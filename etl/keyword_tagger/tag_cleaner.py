import asyncio
import logging

from etl.loggers import setup_logging
from etl.settings import BASE_DIR
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)

TAGS_COUNTED_PATH = BASE_DIR / "etl" / "keyword_tagger" / "tags_counted.json"

EXCLUDE_LIST = [
    "kwalifikowany podpis",
    "platforma zakupowa",
    "podpis kwalifikowany",
    "dostawy",
    "dostawa",
    "category-55321000",
    "ogłoszenie naukowe",
    "ogloszenie naukowe",
    "platformazakupowa.pl",
    "ārstnieciskais",
    "zugangsanlagen für behinderte"
]

async def main():
    tags: dict[str, int] = (await read_json(TAGS_COUNTED_PATH))["tags"]

    tags = {k: v for k, v in tags.items() if k.lower() not in EXCLUDE_LIST}

    await save_json(TAGS_COUNTED_PATH, {"tags": tags})


if __name__ == "__main__":
    asyncio.run(main())