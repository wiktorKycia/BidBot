from __future__ import annotations

import argparse
import json
from pathlib import Path
from textwrap import fill

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT_DIR / "etl" / "keyword_tagger" / "tags.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "analysis_output" / "tags.png"


def load_tag_counts(path: Path) -> dict[str, int]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)

    tags = payload.get("tags", payload)
    if not isinstance(tags, dict):
        raise ValueError("Expected a JSON object with a 'tags' dictionary.")

    counts: dict[str, int] = {}
    for tag, count in tags.items():
        counts[str(tag)] = int(count)
    return counts


def sort_tag_counts(tag_counts: dict[str, int]) -> list[tuple[str, int]]:
    return sorted(tag_counts.items(), key=lambda item: (-item[1], item[0].lower()))


def build_plot(items: list[tuple[str, int]], output_path: Path, title: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not items:
        raise ValueError("No tag data available to plot.")

    labels = [fill(tag, width=42, break_long_words=False, break_on_hyphens=False) for tag, _ in items]
    values = [count for _, count in items]

    height = max(8.0, 0.33 * len(items) + 2.0)
    width = 18.0

    fig, ax = plt.subplots(figsize=(width, height))
    bars = ax.barh(range(len(items)), values, color="#2563EB", edgecolor="#1E3A8A", linewidth=0.6)

    ax.set_yticks(range(len(items)))
    ax.set_yticklabels(labels, fontsize=9)
    ax.invert_yaxis()

    ax.set_xlabel("Liczba wystąpień")
    ax.set_title(title, fontsize=15, pad=14)
    ax.grid(axis="x", linestyle="--", alpha=0.3)
    ax.set_axisbelow(True)

    max_value = max(values)
    label_offset = max_value * 0.01 if max_value else 0.5
    ax.set_xlim(0, max_value * 1.15 if max_value else 1)

    for bar, value in zip(bars, values, strict=True):
        ax.text(
            bar.get_width() + label_offset,
            bar.get_y() + bar.get_height() / 2,
            f"{value}",
            va="center",
            ha="left",
            fontsize=9,
            color="#0F172A",
        )

    fig.tight_layout()
    fig.savefig(output_path, dpi=220, bbox_inches="tight")
    plt.close(fig)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a sorted PNG bar chart from tag counts.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="Path to the tag counts JSON file.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Where to save the PNG chart.")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="How many of the most common tags to plot. Use 0 or a negative number for all tags.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tag_counts = load_tag_counts(args.input)
    sorted_counts = sort_tag_counts(tag_counts)

    if args.limit and args.limit > 0:
        sorted_counts = sorted_counts[: args.limit]

    title = "Najczęstsze tagi"
    if args.limit and args.limit > 0:
        title = f"Najczęstsze tagi — top {min(args.limit, len(tag_counts))}"

    build_plot(sorted_counts, args.output, title)
    print(f"Saved chart to: {args.output}")


if __name__ == "__main__":
    main()
