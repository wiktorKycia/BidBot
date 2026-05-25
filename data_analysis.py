import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
from matplotlib.gridspec import GridSpec
from etl.scrapers.settings import BASE_DIR, PARSED_DIR, ATTACHMENTS_DIR


# ── Paleta kolorów ──────────────────────────────────────────────────────────────
PALETTE = [
    "#2563EB", "#16A34A", "#DC2626", "#D97706", "#7C3AED",
    "#0891B2", "#DB2777", "#65A30D", "#EA580C", "#6B7280",
]
BG = "#F8FAFC"
CARD = "#FFFFFF"
TEXT = "#1E293B"
SUBTEXT = "#64748B"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "axes.facecolor": CARD,
    "figure.facecolor": BG,
    "axes.edgecolor": "#E2E8F0",
    "axes.labelcolor": TEXT,
    "xtick.color": SUBTEXT,
    "ytick.color": SUBTEXT,
    "text.color": TEXT,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": "#E2E8F0",
    "grid.linewidth": 0.6,
})


# ══════════════════════════════════════════════════════════════════════════════════
# 1. WCZYTYWANIE DANYCH
# ══════════════════════════════════════════════════════════════════════════════════

def load_parsed_jsons(parsed_dir: Path) -> list[dict]:
    records = []
    for json_path in parsed_dir.glob("*.json"):
        try:
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)
                data["_json_filename"] = json_path.stem
                records.append(data)
        except Exception as e:
            print(f"[WARN] Nie udało się wczytać {json_path.name}: {e}")
    return records


def classify_source(url: str) -> str:
    if not url:
        return "nieznane"
    url = url.lower()
    if "ezamowienia" in url:
        return "eZamówienia"
    if "platformazakupowa" in url or "platformaofertowa" in url:
        return "Platforma Zakupowa"
    if "ted" in url:
        return "TED"
    return "inne"


def get_ext(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".docx", ".doc", ".docm"}:
        return "Word (.doc/x)"
    if suffix in {".xlsx", ".xls"}:
        return "Excel (.xls/x)"
    if suffix == ".pdf":
        return "PDF"
    if suffix == ".xml":
        return "XML"
    if suffix == ".zip":
        return "ZIP"
    if suffix in {".7z", ".rar"}:
        return "Archiwum (7z/rar)"
    if suffix in {".txt", ".csv"}:
        return "Tekst (txt/csv)"
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".bmp"}:
        return "Obraz"
    if suffix == "":
        return "bez rozszerzenia"
    return f"inne ({suffix})"


# ══════════════════════════════════════════════════════════════════════════════════
# 2. ZBIERANIE STATYSTYK
# ══════════════════════════════════════════════════════════════════════════════════

def collect_stats(records: list[dict], attachments_dir: Path) -> dict:
    stats = {
        "total_offers": len(records),
        "id_mismatches": [],
        "att_counts": [],
        "ext_counter": Counter(),
        "ext_sizes_kb": defaultdict(list),
        "source_counter": Counter(),
        "all_att_sizes_kb": [],
        "zip_not_extracted": 0,
        "subfolder_count": 0,
    }

    for rec in records:
        offer_id = str(rec.get("id", ""))
        json_name = rec.get("_json_filename", "")

        # Sprawdzenie spójności ID ↔ nazwa pliku
        if offer_id != json_name:
            stats["id_mismatches"].append((offer_id, json_name))

        # Źródło
        url = rec.get("scraper_url", "")
        stats["source_counter"][classify_source(url)] += 1

        # Załączniki z JSONa
        attachments = rec.get("scraper_attachments") or []
        downloaded = [a for a in attachments if a.get("downloaded")]
        stats["att_counts"].append(len(downloaded))

        for att in downloaded:
            filename = att.get("filename", "")
            ext_label = get_ext(filename)

            # Rozmiar z dysku (jeśli plik istnieje)
            local_path = att.get("local_path")
            size_kb = None
            if local_path:
                full_path = BASE_DIR / local_path
                if full_path.exists():
                    size_kb = full_path.stat().st_size / 1024

            if size_kb is not None:
                stats["ext_sizes_kb"][ext_label].append(size_kb)
                stats["all_att_sizes_kb"].append(size_kb)
            else:
                # Użyj size_bytes z metadanych jako fallback
                sb = att.get("size_bytes", 0) or 0
                if sb:
                    size_kb = sb / 1024
                    stats["ext_sizes_kb"][ext_label].append(size_kb)
                    stats["all_att_sizes_kb"].append(size_kb)

            stats["ext_counter"][ext_label] += 1

            # ZIPy nierozpakowane
            if att.get("is_zip") and att.get("extracted_status") not in ("success",):
                stats["zip_not_extracted"] += 1

    # Podfoldery w katalogu attachmentów
    if attachments_dir.exists():
        for offer_folder in attachments_dir.iterdir():
            if offer_folder.is_dir():
                sub_dirs = [d for d in offer_folder.iterdir() if d.is_dir()]
                stats["subfolder_count"] += len(sub_dirs)

    return stats


# ══════════════════════════════════════════════════════════════════════════════════
# 3. WYKRESY
# ══════════════════════════════════════════════════════════════════════════════════

def fmt_size(kb: float) -> str:
    if kb >= 1024:
        return f"{kb / 1024:.1f} MB"
    return f"{kb:.1f} KB"


def plot_all(stats: dict):
    total = stats["total_offers"]
    att_counts = stats["att_counts"]
    total_atts = sum(att_counts)
    avg_atts = np.mean(att_counts) if att_counts else 0
    avg_size = np.mean(stats["all_att_sizes_kb"]) if stats["all_att_sizes_kb"] else 0

    fig = plt.figure(figsize=(20, 24), facecolor=BG)
    fig.suptitle("Analiza zebranych przetargów", fontsize=22, fontweight="bold",
                 color=TEXT, y=0.98)

    gs = GridSpec(4, 2, figure=fig, hspace=0.45, wspace=0.35,
                  top=0.95, bottom=0.04, left=0.07, right=0.97)

    # ── KPI cards (górny wiersz) ────────────────────────────────────────────────
    ax_kpi = fig.add_subplot(gs[0, :])
    ax_kpi.axis("off")

    kpis = [
        ("Ofert łącznie", f"{total:,}"),
        ("Załączników łącznie", f"{total_atts:,}"),
        ("Śr. załączników / oferta", f"{avg_atts:.1f}"),
        ("Śr. rozmiar załącznika", fmt_size(avg_size)),
        ("ZIPy nierozpakowane", f"{stats['zip_not_extracted']}"),
        ("Podfoldery (ZIP extract)", f"{stats['subfolder_count']}"),
        ("Błędy ID ↔ plik", f"{len(stats['id_mismatches'])}"),
    ]

    n = len(kpis)
    for i, (label, value) in enumerate(kpis):
        x = i / n
        rect = plt.Rectangle((x + 0.005, 0.05), 1 / n - 0.015, 0.9,
                               facecolor=CARD, edgecolor="#CBD5E1",
                               linewidth=1.2, transform=ax_kpi.transAxes,
                               clip_on=False)
        ax_kpi.add_patch(rect)
        ax_kpi.text(x + 1 / n / 2, 0.65, value,
                    ha="center", va="center", fontsize=18, fontweight="bold",
                    color=PALETTE[i % len(PALETTE)], transform=ax_kpi.transAxes)
        ax_kpi.text(x + 1 / n / 2, 0.25, label,
                    ha="center", va="center", fontsize=9, color=SUBTEXT,
                    transform=ax_kpi.transAxes)

    # ── Wykres kołowy: typy załączników ────────────────────────────────────────
    ax_pie = fig.add_subplot(gs[1, 0])
    ext_labels = list(stats["ext_counter"].keys())
    ext_vals = list(stats["ext_counter"].values())

    if ext_vals:
        sorted_pairs = sorted(zip(ext_vals, ext_labels), reverse=True)
        ext_vals, ext_labels = zip(*sorted_pairs)
        colors = [PALETTE[i % len(PALETTE)] for i in range(len(ext_labels))]
        wedges, texts, autotexts = ax_pie.pie(
            ext_vals, labels=None, autopct="%1.1f%%", colors=colors,
            startangle=140, pctdistance=0.78,
            wedgeprops={"edgecolor": "white", "linewidth": 1.5}
        )
        for at in autotexts:
            at.set_fontsize(8)
        ax_pie.legend(wedges, ext_labels, loc="center left",
                      bbox_to_anchor=(1.0, 0.5), fontsize=9,
                      frameon=False)
    ax_pie.set_title("Typy załączników", fontsize=13, fontweight="bold", pad=12)

    # ── Histogram: liczba załączników na ofertę ─────────────────────────────────
    ax_hist = fig.add_subplot(gs[1, 1])
    if att_counts:
        max_count = max(att_counts)
        bins = range(0, max_count + 2)
        ax_hist.hist(att_counts, bins=bins, color=PALETTE[0], edgecolor="white",
                     linewidth=0.8, rwidth=0.85, align="left")
        ax_hist.axvline(avg_atts, color=PALETTE[2], linestyle="--",
                        linewidth=1.5, label=f"Średnia: {avg_atts:.1f}")
        ax_hist.legend(fontsize=9, frameon=False)
    ax_hist.set_xlabel("Liczba załączników")
    ax_hist.set_ylabel("Liczba ofert")
    ax_hist.set_title("Rozkład liczby załączników na ofertę", fontsize=13, fontweight="bold")
    ax_hist.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))

    # ── Słupkowy: źródła ofert ──────────────────────────────────────────────────
    ax_src = fig.add_subplot(gs[2, 0])
    if stats["source_counter"]:
        src_items = stats["source_counter"].most_common()
        src_labels, src_vals = zip(*src_items)
        bars = ax_src.barh(src_labels, src_vals,
                           color=[PALETTE[i % len(PALETTE)] for i in range(len(src_labels))],
                           edgecolor="white", linewidth=0.8)
        for bar, val in zip(bars, src_vals):
            ax_src.text(bar.get_width() + 0.3, bar.get_y() + bar.get_height() / 2,
                        str(val), va="center", fontsize=9, color=TEXT)
    ax_src.set_xlabel("Liczba ofert")
    ax_src.set_title("Źródła ofert", fontsize=13, fontweight="bold")
    ax_src.invert_yaxis()

    # ── Słupkowy: średnia waga wg typu ──────────────────────────────────────────
    ax_size = fig.add_subplot(gs[2, 1])
    if stats["ext_sizes_kb"]:
        size_items = sorted(
            ((ext, np.mean(sizes)) for ext, sizes in stats["ext_sizes_kb"].items()),
            key=lambda x: x[1], reverse=True
        )
        size_labels, size_means = zip(*size_items)
        bars = ax_size.barh(
            size_labels, [s / 1024 if s >= 1024 else s for s in size_means],
            color=[PALETTE[i % len(PALETTE)] for i in range(len(size_labels))],
            edgecolor="white", linewidth=0.8
        )
        unit = "MB" if any(s >= 1024 for s in size_means) else "KB"
        for bar, val_kb in zip(bars, size_means):
            label = fmt_size(val_kb)
            ax_size.text(bar.get_width() + 0.005, bar.get_y() + bar.get_height() / 2,
                         label, va="center", fontsize=8.5, color=TEXT)
        ax_size.set_xlabel(f"Średni rozmiar ({unit})")
        ax_size.set_title("Średnia waga załącznika wg typu", fontsize=13, fontweight="bold")
        ax_size.invert_yaxis()

    # ── Sumaryczny rozmiar wg typu ──────────────────────────────────────────────
    ax_total = fig.add_subplot(gs[3, 0])
    if stats["ext_sizes_kb"]:
        total_items = sorted(
            ((ext, sum(sizes)) for ext, sizes in stats["ext_sizes_kb"].items()),
            key=lambda x: x[1], reverse=True
        )
        t_labels, t_vals_kb = zip(*total_items)
        t_vals = [v / 1024 for v in t_vals_kb]
        bars = ax_total.barh(
            t_labels, t_vals,
            color=[PALETTE[i % len(PALETTE)] for i in range(len(t_labels))],
            edgecolor="white", linewidth=0.8
        )
        for bar, val_kb in zip(bars, t_vals_kb):
            ax_total.text(bar.get_width() + 0.005, bar.get_y() + bar.get_height() / 2,
                          fmt_size(val_kb), va="center", fontsize=8.5, color=TEXT)
        ax_total.set_xlabel("Łączny rozmiar (MB)")
        ax_total.set_title("Łączna waga załączników wg typu", fontsize=13, fontweight="bold")
        ax_total.invert_yaxis()

    # ── Tabela: błędy ID ↔ plik JSON ───────────────────────────────────────────
    ax_tbl = fig.add_subplot(gs[3, 1])
    ax_tbl.axis("off")

    mismatches = stats["id_mismatches"]
    if not mismatches:
        ax_tbl.text(0.5, 0.5, "✓  Brak niezgodności ID ↔ nazwa pliku JSON",
                    ha="center", va="center", fontsize=12,
                    color="#16A34A", fontweight="bold",
                    transform=ax_tbl.transAxes)
    else:
        show = mismatches[:10]
        table_data = [["ID w JSON", "Nazwa pliku"]] + [[a, b] for a, b in show]
        tbl = ax_tbl.table(cellText=table_data[1:], colLabels=table_data[0],
                            loc="center", cellLoc="left")
        tbl.auto_set_font_size(False)
        tbl.set_fontsize(8)
        tbl.scale(1, 1.4)
        for (r, c), cell in tbl.get_celld().items():
            cell.set_edgecolor("#E2E8F0")
            if r == 0:
                cell.set_facecolor("#EFF6FF")
                cell.set_text_props(fontweight="bold")
            else:
                cell.set_facecolor(CARD)
        if len(mismatches) > 10:
            ax_tbl.text(0.5, 0.02, f"... i {len(mismatches) - 10} więcej",
                        ha="center", fontsize=8, color=SUBTEXT,
                        transform=ax_tbl.transAxes)
    ax_tbl.set_title("Spójność ID ↔ nazwa pliku JSON", fontsize=13,
                     fontweight="bold", pad=10)

    out_path = BASE_DIR / "tender_analysis.png"
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=BG)
    print(f"\n✅  Wykres zapisano: {out_path}")
    plt.show()


# ══════════════════════════════════════════════════════════════════════════════════
# 4. MAIN
# ══════════════════════════════════════════════════════════════════════════════════

def main():
    if not PARSED_DIR.exists():
        print(f"[ERROR] Nie znaleziono katalogu: {PARSED_DIR}")
        return

    print(f"Wczytywanie plików JSON z: {PARSED_DIR}")
    records = load_parsed_jsons(PARSED_DIR)

    if not records:
        print("[WARN] Brak plików JSON do analizy.")
        return

    print(f"Znaleziono {len(records)} rekordów. Zbieranie statystyk...")
    stats = collect_stats(records, ATTACHMENTS_DIR)

    # Wydruk tekstowy
    print("\n" + "═" * 50)
    print(f"  Łączna liczba ofert:          {stats['total_offers']:>6}")
    print(f"  Łączna liczba załączników:    {sum(stats['att_counts']):>6}")
    print(f"  Średnio załączników/oferta:   {np.mean(stats['att_counts']):.2f}" if stats["att_counts"] else "  Brak danych")
    print(f"  ZIPy nierozpakowane:          {stats['zip_not_extracted']:>6}")
    print(f"  Podfoldery (rozpakowane):     {stats['subfolder_count']:>6}")
    print(f"  Niezgodności ID ↔ plik:       {len(stats['id_mismatches']):>6}")
    print("\n  Typy załączników:")
    for ext, cnt in stats["ext_counter"].most_common():
        sizes = stats["ext_sizes_kb"].get(ext, [])
        avg = np.mean(sizes) if sizes else 0
        print(f"    {ext:<25} {cnt:>5} szt.  śr. {fmt_size(avg)}")
    print("═" * 50 + "\n")

    plot_all(stats)


if __name__ == "__main__":
    main()