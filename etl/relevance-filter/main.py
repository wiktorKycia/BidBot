import asyncio
import os
from pathlib import Path
import logging
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI
from etl.utils import read_json
from etl.settings import setup_logging, require_openai_api_key, MODEL, PARSED_DIR


OPENAI_API_KEY = require_openai_api_key()


setup_logging()
logger = logging.getLogger(__name__)

prompt = """
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
"""

async def tag_file(filename: str):
    data: dict = await read_json(PARSED_DIR / filename)
    if len(data['enrichment']['tags']) > 0: # nie trzeba tagować, bo tagi już są
        return None
    
    


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
