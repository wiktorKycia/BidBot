import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = ROOT_DIR / "etl" / "keyword_tagger" / "tags_counted.json"
DEFAULT_JSON_OUTPUT = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.json"


class TagCount(BaseModel):
    tag: str = Field(description="The exact original tag name")
    count: int = Field(description="The count of the tag")

class IndustryGroup(BaseModel):
    industry: str = Field(description="The name of the industry")
    tags: list[TagCount] = Field(description="List of tags belonging to this industry")

class GroupedTagsOutput(BaseModel):
    industries: list[IndustryGroup] = Field(description="List of clustered industries")


SYSTEM_PROMPT = SystemMessage(
    content=(
        "You are an expert taxonomy analyst for public procurement tags."
        "Group each input tag into exactly one broad industry."
        "Use human-readable industry names such as IT, budownictwo, transport, administracja, edukacja, medycyna, energetyka, usługi, rolnictwo, "
        "finanse, inżynieria, and Inne when needed. "
        "Preserve every tag key exactly as it appears in the input. Do not rename tags, split tags, or invent new tag text. "
        "Preserve counts exactly. Each original tag must appear exactly once under one industry. "
        "Prefer a moderate number of broad industries rather than many tiny ones. "
        "Return only structured JSON matching the schema."
    )
)


def normalize_tag_counts(payload: dict) -> dict[str, int]:
    tags = payload.get("tags", payload)
    if not isinstance(tags, dict):
        raise ValueError("Expected the input JSON to contain a tags dictionary.")

    counts: dict[str, int] = {}
    for tag, count in tags.items():
        counts[str(tag).strip()] = int(count)
    return counts


async def load_tag_counts(path: Path) -> dict[str, int]:
    payload = await read_json(path)
    return normalize_tag_counts(payload)


def sort_tag_counts(tag_counts: dict[str, int]) -> dict[str, int]:
    return dict(sorted(tag_counts.items(), key=lambda item: (-item[1], item[0].lower())))


def build_llm() -> Any:
    api_key = require_openai_api_key()
    return ChatOpenAI(model=MODEL, api_key=lambda: api_key, temperature=0).with_structured_output(GroupedTagsOutput)


async def group_tags_with_llm(tag_counts: dict[str, int]) -> dict[str, dict[str, int]]:
    llm = build_llm()
    sorted_tags = sort_tag_counts(tag_counts)

    prompt = (
        "Group the following tags into industries. "
        "Return every original tag exactly once. "
        "Use the exact original tag spelling as subcategory keys and keep the counts unchanged.\n\n"
        f"Input tags with counts:\n{json.dumps(sorted_tags, ensure_ascii=False, indent=2)}"
    )

    logger.info("Sending request to LLM...")
    response: GroupedTagsOutput = await llm.ainvoke([SYSTEM_PROMPT, HumanMessage(content=prompt)])
    
    # Dump model response back to the standard grouped dictionary
    grouped = {}
    for ind in response.industries:
        grouped[ind.industry] = {t.tag: t.count for t in ind.tags}

    return grouped


async def main() -> None:
    DEFAULT_JSON_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    
    tag_counts = await load_tag_counts(DEFAULT_INPUT)
    grouped = await group_tags_with_llm(tag_counts)

    await save_json(DEFAULT_JSON_OUTPUT, grouped)
    logger.info("Saved grouped JSON to %s", DEFAULT_JSON_OUTPUT)


if __name__ == "__main__":
    asyncio.run(main())
