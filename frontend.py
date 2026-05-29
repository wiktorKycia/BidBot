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
        st.rerun()

with tab_analysis:
    import json
    from pathlib import Path
    import pandas as pd
    import altair as alt

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

            # Pie / Donut Chart
            pie_data = [{"Branża": ind, "Liczba": sum(tags.values())} for ind, tags in grouped.items()]
            pie_df = pd.DataFrame(pie_data)

            pie_chart = (
                alt.Chart(pie_df)
                .mark_arc(innerRadius=0)
                .encode(
                    theta=alt.Theta("Liczba:Q"),
                    color=alt.Color("Branża:N", sort=alt.EncodingSortField(field="Liczba", order="descending")),
                    tooltip=["Branża", "Liczba"]
                )
                .properties(title="Udział tagów według branż")
                .configure_title(fontSize=20)
                .configure_legend(titleFontSize=16, labelFontSize=14)
            )
            
            st.altair_chart(pie_chart, width='stretch')
            st.divider()

            # Bar Charts
            for industry, tags in grouped.items():
                top_tags = list(tags.items())[:5]
                tags_data = [{"Tag": tag, "Liczba": count} for tag, count in top_tags]
                tags_df = pd.DataFrame(tags_data)
                
                base_chart = alt.Chart(tags_df).encode(
                    x=alt.X("Liczba:Q", title="Liczba wystąpień", axis=alt.Axis(titleFontSize=16, labelFontSize=14, labelLineHeight=2)),
                    y=alt.Y("Tag:N", title="", sort=alt.EncodingSortField(field="Liczba", order="descending"), axis=alt.Axis(labelFontSize=14)),
                    tooltip=["Tag", "Liczba"]
                )
                
                bar = base_chart.mark_bar()
                text = base_chart.mark_text(align="left", baseline="middle", dx=3, fontSize=16, color="white").encode(text="Liczba:Q")
                
                chart = (
                    (bar + text)
                    .properties(title=f"{industry} — {sum(tags.values())} tagów")
                    .interactive()
                    .configure_title(fontSize=20)
                )
                st.altair_chart(chart, width='stretch')
