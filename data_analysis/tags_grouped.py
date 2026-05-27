from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from textwrap import fill
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from matplotlib.gridspec import GridSpec
from pydantic import BaseModel, Field

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.utils import read_json, save_json

setup_logging()
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT_DIR / "etl" / "keyword_tagger" / "tags.json"
DEFAULT_JSON_OUTPUT = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.json"
DEFAULT_PNG_OUTPUT = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.png"


class GroupedTagsOutput(BaseModel):
    industries: dict[str, dict[str, int]] = Field(description="Mapping from industry name to exact original tag names with their counts.")


SYSTEM_PROMPT = SystemMessage(
    content=(
        "You are an expert taxonomy analyst for public procurement tags. "
        "Group each input tag into exactly one broad industry. "
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


def sort_grouped_industries(grouped: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    sorted_industries: list[tuple[str, dict[str, int], int]] = []
    for industry, tags in grouped.items():
        sorted_tags = sort_tag_counts(tags)
        total = sum(sorted_tags.values())
        if total > 0:
            sorted_industries.append((industry, sorted_tags, total))

    sorted_industries.sort(key=lambda item: (-item[2], item[0].lower()))
    return {industry: tags for industry, tags, _ in sorted_industries}


def flatten_grouped_industries(grouped: dict[str, dict[str, int]]) -> dict[str, int]:
    flattened: dict[str, int] = {}
    for _industry, tags in grouped.items():
        for tag, count in tags.items():
            if tag in flattened:
                raise ValueError(f"Tag {tag!r} appears in multiple industries.")
            flattened[tag] = int(count)
    return flattened


def validate_grouping(source_counts: dict[str, int], grouped: dict[str, dict[str, int]]) -> None:
    flattened = flatten_grouped_industries(grouped)
    source_keys = set(source_counts)
    grouped_keys = set(flattened)

    missing = sorted(source_keys - grouped_keys)
    extra = sorted(grouped_keys - source_keys)
    mismatched = sorted(tag for tag in source_keys & grouped_keys if int(source_counts[tag]) != int(flattened[tag]))

    if missing or extra or mismatched:
        problems: list[str] = []
        if missing:
            problems.append(f"missing tags: {missing[:20]}")
        if extra:
            problems.append(f"unexpected tags: {extra[:20]}")
        if mismatched:
            problems.append(f"mismatched counts: {mismatched[:20]}")
        raise ValueError("Invalid grouping returned by model: " + "; ".join(problems))


def build_llm() -> Any:
    require_openai_api_key()
    return ChatOpenAI(model=MODEL, temperature=0).with_structured_output(GroupedTagsOutput)


async def group_tags_with_llm(tag_counts: dict[str, int], max_retries: int = 3) -> dict[str, dict[str, int]]:
    llm = build_llm()
    sorted_tags = sort_tag_counts(tag_counts)

    prompt_base = (
        "Group the following tags into industries. "
        "Return every original tag exactly once. "
        "Use the exact original tag spelling as subcategory keys and keep the counts unchanged.\n\n"
        f"Input tags with counts:\n{json.dumps(sorted_tags, ensure_ascii=False, indent=2)}"
    )

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        prompt = prompt_base
        if last_error is not None:
            prompt += f"\n\nThe previous attempt failed validation with this error: {last_error}. Fix the grouping and return only valid JSON."

        try:
            response = await llm.ainvoke([SYSTEM_PROMPT, HumanMessage(content=prompt)])
            grouped = response.industries
            validate_grouping(sorted_tags, grouped)
            return sort_grouped_industries(grouped)
        except Exception as exc:
            last_error = exc
            logger.warning("Grouping attempt %s/%s failed: %s", attempt, max_retries, exc)

    raise RuntimeError(f"Failed to group tags after {max_retries} attempts: {last_error}")


def industry_totals(grouped: dict[str, dict[str, int]]) -> dict[str, int]:
    return {industry: sum(tags.values()) for industry, tags in grouped.items()}


def _wrap_label(label: str, width: int = 36) -> str:
    return fill(label, width=width, break_long_words=False, break_on_hyphens=False)


def plot_grouped_tags(grouped: dict[str, dict[str, int]], output_path: Path, title: str = "Tagi pogrupowane według branż") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not grouped:
        raise ValueError("No grouped data available to plot.")

    industries = list(grouped.keys())
    totals = [sum(tags.values()) for tags in grouped.values()]
    cmap = plt.get_cmap("tab20")
    pie_colors = [cmap(i) for i in range(cmap.N)]

    industry_heights = [max(2.6, 0.35 * len(tags) + 1.2) for tags in grouped.values()]
    pie_height = max(5.5, 0.35 * len(industries) + 3.5)
    total_height = pie_height + sum(industry_heights)

    fig = plt.figure(figsize=(24, total_height), facecolor="#F8FAFC")
    fig.suptitle(title, fontsize=22, fontweight="bold", y=0.995)

    gs = GridSpec(
        nrows=1 + len(industries),
        ncols=1,
        figure=fig,
        height_ratios=[pie_height] + industry_heights,
        hspace=0.7,
        top=0.965,
        bottom=0.02,
        left=0.05,
        right=0.94,
    )

    ax_pie = fig.add_subplot(gs[0, 0])
    wedges, _, autotexts = ax_pie.pie(
        totals,
        labels=None,
        autopct=lambda pct: f"{pct:.1f}%" if pct >= 2 else "",
        startangle=90,
        counterclock=False,
        colors=[pie_colors[i % len(pie_colors)] for i in range(len(industries))],
        wedgeprops={"linewidth": 1, "edgecolor": "white"},
        textprops={"fontsize": 10},
    )
    ax_pie.set_title("Udział tagów według branż", fontsize=16, pad=10)
    ax_pie.axis("equal")

    legend_labels = [f"{industry} — {total}" for industry, total in zip(industries, totals, strict=True)]
    ax_pie.legend(
        wedges,
        legend_labels,
        title="Branże",
        loc="center left",
        bbox_to_anchor=(1.0, 0.5),
        fontsize=9,
        title_fontsize=10,
        frameon=False,
    )

    for text in autotexts:
        text.set_color("#0F172A")
        text.set_fontsize(9)

    for row, (industry, tags) in enumerate(grouped.items(), start=1):
        ax = fig.add_subplot(gs[row, 0])
        sorted_tags = list(sort_tag_counts(tags).items())
        labels = [_wrap_label(tag) for tag, _ in sorted_tags]
        values = [count for _, count in sorted_tags]

        bar_color = pie_colors[(row - 1) % len(pie_colors)]
        bars = ax.barh(range(len(values)), values, color=bar_color, edgecolor="#1E293B", linewidth=0.4)
        ax.set_yticks(range(len(values)))
        ax.set_yticklabels(labels, fontsize=8)
        ax.invert_yaxis()
        ax.grid(axis="x", linestyle="--", alpha=0.3)
        ax.set_axisbelow(True)

        max_value = max(values) if values else 0
        ax.set_xlim(0, max_value * 1.2 if max_value else 1)
        ax.set_xlabel("Liczba wystąpień")
        ax.set_title(f"{industry} — {sum(values)} tagów", fontsize=14, pad=8)

        label_offset = max_value * 0.01 if max_value else 0.5
        for bar, value in zip(bars, values, strict=True):
            ax.text(
                bar.get_width() + label_offset,
                bar.get_y() + bar.get_height() / 2,
                f"{value}",
                va="center",
                ha="left",
                fontsize=8,
                color="#0F172A",
            )

    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Group tag counts into industries and export JSON + PNG visuals.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to the source tags.json file.")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON_OUTPUT, help="Where to save tags_by_industries.json.")
    parser.add_argument("--png-output", type=Path, default=DEFAULT_PNG_OUTPUT, help="Where to save the PNG chart.")
    parser.add_argument("--max-retries", type=int, default=3, help="How many times to retry the LLM grouping if validation fails.")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    tag_counts = await load_tag_counts(args.input)
    grouped = await group_tags_with_llm(tag_counts, max_retries=args.max_retries)

    await save_json(args.json_output, grouped)
    plot_grouped_tags(grouped, args.png_output)

    logger.info("Saved grouped JSON to %s", args.json_output)
    logger.info("Saved chart PNG to %s", args.png_output)


if __name__ == "__main__":
    asyncio.run(main())
