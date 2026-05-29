from pathlib import Path
import json

BASE_DIR = Path(__file__).resolve().parent.parent
CHROMA_DB_PATH = BASE_DIR / "etl" / "vector_db" / "chroma_langchain_db"
TAGS_PATH = BASE_DIR / "etl" / "keyword_tagger" / "tags_counted.json"
INDUSTRIES_PATH = BASE_DIR / "data_analysis" / "analysis_output" / "counted_industries.json"

def get_all_tags() -> str:
    with open(TAGS_PATH, "r", encoding="utf-8") as f:
        _tags_data: dict[str, int] = json.loads(f.read())["tags"]
        all_tags = ", ".join(list(_tags_data.keys())) if isinstance(_tags_data, dict) else ""
    return all_tags
