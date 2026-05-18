
from pathlib import Path
from etl.settings import MODEL, require_openai_api_key


OPENAI_API_KEY = require_openai_api_key()

DATA_PATH = Path(__file__).resolve().parent.parent.parent / "data"
PARSED_JSON_PATH = DATA_PATH / "parsed"

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_PATH = LOG_DIR / "vector_db.log"




def main():
    pass
    '''
    read all the .json documents asynchronously
    check if they have tags
    if not perform a query to the llm that will create them
    '''

if __name__ == "__main__":
    main()
