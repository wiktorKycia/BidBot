import asyncio
import logging
from pathlib import Path
from textwrap import fill

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

from etl.loggers import setup_logging
from etl.utils import read_json

setup_logging()
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_JSON_INPUT = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.json"
DEFAULT_PNG_OUTPUT = Path(__file__).resolve().parent / "analysis_output" / "tags_by_industries.png"


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


def _wrap_label(label: str, width: int = 36) -> str:
    return fill(label, width=width, break_long_words=False, break_on_hyphens=False)


def plot_grouped_tags(grouped: dict[str, dict[str, int]], output_path: Path, title: str = "Tagi pogrupowane według branż") -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    grouped = sort_grouped_industries(grouped)

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
        sorted_tags = list(tags.items())
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


async def main() -> None:
    grouped = await read_json(DEFAULT_JSON_INPUT)
    plot_grouped_tags(grouped, DEFAULT_PNG_OUTPUT)

    logger.info("Saved chart PNG to %s", DEFAULT_PNG_OUTPUT)


if __name__ == "__main__":
    asyncio.run(main())
