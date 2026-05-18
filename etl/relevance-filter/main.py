import os
from pathlib import Path
from dotenv import load_dotenv

DOTENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(DOTENV_PATH)

def require_openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"OPENAI_API_KEY is not set. Configure it in the environment or in {DOTENV_PATH}.")
    return api_key


OPENAI_API_KEY = require_openai_api_key()

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data"
PARSED_JSON_PATH = DATA_PATH / "parsed"

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_PATH = LOG_DIR / "vector_db.log"

MODEL = "gpt-5.4-nano"


def main():
    pass
    '''
    read all the .json documents asynchronously
    check if they have tags
    if not perform a query to the llm that will create them
    '''

if __name__ == "__main__":
    main()
