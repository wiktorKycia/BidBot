from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DB_PATH = BASE_DIR / "etl" / "vector_db" / "chroma_langchain_db"
TAGS_PATH = BASE_DIR / "etl" / "keyword_tagger" / "tags.json"
EMBEDDING_MODEL = "text-embedding-3-small"
