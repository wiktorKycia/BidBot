import asyncio
import json
import logging

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.scrapers.settings import PARSED_DIR
from etl.utils import read_json, save_json

OPENAI_API_KEY = require_openai_api_key()

setup_logging()
logger = logging.getLogger(__name__)


class TagsOutput(BaseModel):
    tags: list[str] = Field(description="List of 3 to 5 high-quality keywords")


llm = ChatOpenAI(model=MODEL, api_key=OPENAI_API_KEY).with_structured_output(TagsOutput)
system_message = SystemMessage("""
You are an expert in public procurement and document indexing.
Your task is to analyze a JSON document representing a public procurement offer and generate a list of 3 to 5 high-quality keywords.

Guidelines for keywords:
1. Focus on the core subject of the procurement (what is being bought or built).
2. Prioritize terms from the 'title' and 'description' fields.
3. Keywords should be concise (1-3 words each).
4. Keywords should be in the same language as the document's primary content (usually Polish or English).
5. These keywords will be used for indexing and as tags for future searches.
6. Return ONLY a valid JSON list of strings.

Input: A JSON document of a procurement offer.
Output: ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"]
""")


async def tag_file(filename: str, semaphore: asyncio.Semaphore) -> int:
    async with semaphore:
        filepath = PARSED_DIR / filename
        data: dict = await read_json(filepath)
        try:
            if len(data["enrichment"]["tags"]) > 0:  # nie trzeba tagować, bo tagi już są
                return 0
        except KeyError as e:
            logger.info(f"Niepełna struktura w pliku: {filename}, wyjątek {e!r} obługiwany")
            data["enrichment"]: dict = {"tags": []}

        response = await llm.ainvoke([system_message, HumanMessage(content=json.dumps(data))])
        tags = response.tags
        logger.debug(f"Otagowano {filename} tagami: {tags}")

        data["enrichment"]["tags"] = tags

        await save_json(filepath, data)
        return 1  # helps count how many files were tagged


async def main():
    filenames = [f.name for f in PARSED_DIR.iterdir() if f.is_file()]

    semaphore = asyncio.Semaphore(50)  # Limit concurrent operations
    tasks = [tag_file(filename, semaphore) for filename in filenames]

    results = await asyncio.gather(*tasks, return_exceptions=True)
    tagged_correctly = 0
    for res in results:
        if isinstance(res, Exception):
            logger.exception(f"Błąd przy tagowaniu pliku: {res!r}")
        else:
            tagged_correctly += res

    logger.info(f"Otagowano {tagged_correctly} plików")


if __name__ == "__main__":
    asyncio.run(main())
