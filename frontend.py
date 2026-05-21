import requests
import streamlit as st

API_URL = "http://localhost:8000/search"

st.set_page_config(page_title="BidBot", layout="wide")
st.title("BidBot")

search_query = st.text_input("Szukaj w bazie:")
search_button = st.button("Szukaj")

if search_button or search_query:
    with st.spinner("Odpytuję bazę..."):
        try:
            res = requests.post(API_URL, json={"query": search_query})
            res.raise_for_status()
            results = res.json()
        except Exception as e:
            st.error(f"Krytyczny błąd: {e}")
            results = []

    if not results:
        st.error("Baza zwróciła 0 wyników.")
    else:
        st.success(f"Znaleziono przetargów: {len(results)}")

        for index, item in enumerate(results):
            title = item.get("title", "Brak tytułu")
            offer_id = item.get("offer_id", "Brak ID")
            score = item.get("score", 0.0)
            buyer = item.get("buyer", "Brak danych")
            deadline = item.get("deadline", "Brak danych")
            source_url = item.get("source_url", "Brak linku")
            description = item.get("description", "Brak opisu")
            full_data = item.get("full_data", "")
            raw_text = item.get("raw_text_preview", "")

            with st.expander(f"📑 {title} | ID: {offer_id} | Score: {score:.4f}"):
                tab1, tab2 = st.tabs(["Podstawowe Info", "JSON"])

                with tab1:
                    st.markdown(f'<span style="font-size:small;">**Zamawiający:** {buyer}</span>', unsafe_allow_html=True)
                    st.markdown(f'<span style="font-size:small;">**Termin:** {deadline}</span>', unsafe_allow_html=True)
                    st.markdown(f'<span style="font-size:small;">**Link źródłowy:** [{source_url}]({source_url})</span>', unsafe_allow_html=True)
                    st.markdown('<span style="font-size:small;">---</span>', unsafe_allow_html=True)
                    st.markdown('<span style="font-size:small;">**Opis / Zakres:**</span>', unsafe_allow_html=True)
                    st.markdown(f'<p style="font-size:small; white-space:pre-wrap;">{description}</p>', unsafe_allow_html=True)

                with tab2:
                    if full_data:
                        st.markdown(f"**Źródło:** `data/parsed/...{offer_id}.json`")
                        st.code(full_data, language="json")
                    else:
                        st.warning("Nie znalazłem pliku JSON o takim ID w data/parsed.")

                    if raw_text:
                        st.markdown("**Fragmenty tekstu (ChromaDB):**")
                        st.text_area(label="Fragmenty tekstu", value=raw_text, height=300, key=f"text_{index}", label_visibility="collapsed")
