import os
from pathlib import Path

from dotenv import load_dotenv

MODEL = "gpt-5.4-nano"
EMBEDDING_MODEL = "text-embedding-3-small"

DOTENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(DOTENV_PATH)


def require_openai_api_key() -> str:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(f"OPENAI_API_KEY is not set. Configure it in the environment or in {DOTENV_PATH}.")
    return api_key
