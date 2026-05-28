import asyncio
import logging
from pathlib import Path

from etl.loggers import setup_logging
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
COUNTED_TAGS = ROOT_DIR / "etl" / "keyword_tagger" / "tags_counted.json"
GROUPED_TAGS = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.json"


def normalize_tag_counts(tags: dict) -> dict[str, int]:
    # tags = payload.get("tags", payload)
    if not isinstance(tags, dict):
        raise ValueError("Expected the input JSON to contain a tags dictionary.")

    counts: dict[str, int] = {}
    for tag, count in tags.items():
        counts[str(tag).strip()] = int(count)
    return counts


async def load_tag_counts(path: Path) -> dict[str, int]:
    payload = (await read_json(path))["tags"]
    return normalize_tag_counts(payload)


async def load_grouped_tags(path: Path) -> dict[str, dict[str, int]]:
    groups: dict[str, dict[str, int]] = await read_json(path)
    for group, tags in groups.items():
        groups[group] = normalize_tag_counts(tags)
    return groups


def correct_numbers(original_counted_tags: dict[str, int], tags_grouped_by_llm: dict[str, dict[str, int]]) -> tuple[dict[str, dict[str, int]], int]:
    seen_tags: set = set()
    corrected: int = 0
    for group, tags in tags_grouped_by_llm.items():
        selected_tags: dict[str, int] = {}
        for tag in list(tags.keys()):  # Use list() to avoid RuntimeError when deleting during iteration
            if tag in seen_tags or tag not in original_counted_tags: # wykluczenie duplikatów i tych co sobie llm dorobił
                continue
            elif tags[tag] != original_counted_tags[tag]:
                corrected += 1
                selected_tags[tag] = original_counted_tags[tag]
                seen_tags.add(tag)
            else:
                selected_tags[tag] = original_counted_tags[tag]
                seen_tags.add(tag)
        tags_grouped_by_llm[group] = selected_tags

    return tags_grouped_by_llm, corrected


async def main() -> None:
    GROUPED_TAGS.parent.mkdir(parents=True, exist_ok=True)

    tag_counts = await load_tag_counts(COUNTED_TAGS)
    grouped_tags = await load_grouped_tags(GROUPED_TAGS)

    parsed, corrected = correct_numbers(tag_counts, grouped_tags)

    await save_json(GROUPED_TAGS, parsed)
    logger.info("Saved grouped JSON to %s", GROUPED_TAGS)
    logger.info(f"Corrected faulty counted tags: {corrected}")


if __name__ == "__main__":
    asyncio.run(main())
