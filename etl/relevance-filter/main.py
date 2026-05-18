import asyncio
import os
from pathlib import Path
import logging

from etl.utils import read_json
from etl.settings import setup_logging, require_openai_api_key, MODEL, PARSED_DIR


OPENAI_API_KEY = require_openai_api_key()


setup_logging()
logger = logging.getLogger(__name__)

async def tag_file(filename: str):
    data: dict = await read_json(PARSED_DIR / filename)
    if len(data['enrichment']['tags']) > 0:
        return None
    raise Exception()


async def main():
    pass
    '''
    read all the .json documents asynchronously
    check if they have tags
    if not perform a query to the llm that will create them
    '''
    filenames = os.listdir(PARSED_DIR)

    tasks = [tag_file(filename) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    errors_count = 0
    for res in results:
        if isinstance(res, Exception):
            errors_count+=1
            logger.error(f"Błąd przy tagowaniu pliku: {res!r}")
    logger.info(f"Ilość błędów: {errors_count}")

if __name__ == "__main__":
    asyncio.run(main())
