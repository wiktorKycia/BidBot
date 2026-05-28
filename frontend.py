import logging
import os

import requests
import streamlit as st

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("frontend")

BASE_URL = os.getenv("BACKEND_URL", "http://backend:8000")
API_URL = f"{BASE_URL}/chat"

st.set_page_config(page_title="BidBot Chat", layout="wide")
st.title("BidBot")

tab_chat, tab_analysis = st.tabs(["Czat", "Analiza danych"])

with tab_chat:
    if "messages" not in st.session_state:
        st.session_state.messages = []

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if user_input := st.chat_input("Zadaj pytanie o przetargi..."):
        with st.chat_message("user"):
            st.markdown(user_input)

        st.session_state.messages.append({"role": "user", "content": user_input})

        api_history = []
        history_messages = st.session_state.messages[:-1]
        for idx, msg in enumerate(history_messages):
            if msg["role"] == "user":
                # Search for the subsequent assistant response
                assistant_content = ""
                for next_msg in history_messages[idx + 1 :]:
                    if next_msg["role"] == "assistant":
                        assistant_content = next_msg["content"]
                        break
                    elif next_msg["role"] == "user":
                        # If another user message appears before an assistant response, ignore this unpaired message
                        break
                if assistant_content:
                    api_history.append({"user": msg["content"], "assistant": assistant_content})

        payload = {"message": user_input, "history": api_history}

        with st.chat_message("assistant"), st.spinner("Myślę..."):
            try:
                response = requests.post(API_URL, json=payload, timeout=30)
                response.raise_for_status()
                answer = response.json()["answer"]
                st.markdown(answer)
                st.session_state.messages.append({"role": "assistant", "content": answer})
            except requests.exceptions.ConnectionError as e:
                logger.error(f"❌ Connection error to {API_URL}: {e}", exc_info=True)
                st.error("❌ Błąd połączenia z serwerem. Spróbuj ponownie później.")
            except requests.exceptions.HTTPError:
                logger.error(f"❌ HTTP error from API [status={response.status_code}]: {response.text}", exc_info=True)
                st.error("❌ Wystąpił błąd serwera podczas przetwarzania żądania.")
            except Exception as e:
                logger.exception(f"❌ Unexpected error in frontend: {e}")
                st.error("❌ Wystąpił nieoczekiwany błąd. Spróbuj ponownie później.")

with tab_analysis:
    import json
    from pathlib import Path
    from textwrap import fill
    import matplotlib.pyplot as plt

    st.header("Analiza Tagów")
    
    json_path = Path(__file__).resolve().parent / "data_analysis" / "analysis_output" / "tags_by_industries.json"
    if not json_path.exists():
        st.warning(f"Brak danych do analizy. Nie znaleziono pliku:\n`{json_path}`")
    else:
        with open(json_path, "r", encoding="utf-8") as f:
            grouped = json.load(f)

        if not grouped:
            st.warning("Brak zgrupowanych danych.")
        else:
            # Sort indusries and tags
            sorted_industries = []
            for ind, tags in grouped.items():
                stags = dict(sorted(tags.items(), key=lambda item: (-item[1], item[0].lower())))
                tot = sum(stags.values())
                if tot > 0:
                    sorted_industries.append((ind, stags, tot))
            
            sorted_industries.sort(key=lambda item: (-item[2], item[0].lower()))
            grouped = {ind: tags for ind, tags, _ in sorted_industries}

            industries = list(grouped.keys())
            totals = [sum(tags.values()) for tags in grouped.values()]
            cmap = plt.get_cmap("tab20")
            pie_colors = [cmap(i) for i in range(cmap.N)]

            # Pie Chart
            fig_pie, ax_pie = plt.subplots(figsize=(8, 6))
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
            ax_pie.set_title("Udział tagów według branż")
            ax_pie.axis("equal")
            
            legend_labels = [f"{ind} — {tot}" for ind, tot in zip(industries, totals)]
            ax_pie.legend(wedges, legend_labels, title="Branże", loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=9)
            
            st.pyplot(fig_pie)
            st.divider()

            # Bar Charts
            for row, (industry, tags) in enumerate(grouped.items()):
                labels = [fill(tag, width=36) for tag in tags.keys()]
                values = list(tags.values())
                
                height = max(3.0, 0.4 * len(tags))
                fig_bar, ax = plt.subplots(figsize=(10, height))
                bar_color = pie_colors[row % len(pie_colors)]
                
                bars = ax.barh(range(len(values)), values, color=bar_color, edgecolor="#1E293B", linewidth=0.4)
                ax.set_yticks(range(len(values)))
                ax.set_yticklabels(labels, fontsize=8)
                ax.invert_yaxis()
                ax.grid(axis="x", linestyle="--", alpha=0.3)
                ax.set_axisbelow(True)

                max_val = max(values) if values else 0
                ax.set_xlim(0, max_val * 1.2 if max_val else 1)
                ax.set_xlabel("Liczba wystąpień")
                ax.set_title(f"{industry} — {sum(values)} tagów")
                
                for bar, val in zip(bars, values):
                    ax.text(
                        bar.get_width() + (max_val * 0.01 if max_val else 0.5),
                        bar.get_y() + bar.get_height() / 2,
                        str(val),
                        va="center",
                        ha="left",
                        fontsize=8,
                        color="#0F172A",
                    )
                
                st.pyplot(fig_bar)
