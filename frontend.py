import requests
import streamlit as st

API_URL = "http://localhost:8000/search"

st.set_page_config(page_title="BidBot - Przeglądarka Przetargów", layout="wide")

st.title("🏛️ BidBot - Znalezione Przetargi")
st.markdown("Przeglądaj, filtruj i analizuj przetargi za pośrednictwem API.")

with st.sidebar:
    st.header("🔍 Filtrowanie")
    search_query = st.text_input("Szukaj w treści...", placeholder="np. system informatyczny")

    category = st.selectbox("Kategoria branżowa", ["Dowolna", "Healthcare", "AI", "Software", "Hardware"])

    min_score = st.slider("Minimalny Relevance Score", 0.0, 1.0, 0.2, 0.05)

    source_filter = st.selectbox("Platforma publikacji", ["Wszystkie", "Biuletyn Zamówień Publicznych", "TED", "Platforma zakupowa", "Nieznane"])

    search_button = st.button("Szukaj przetargów", use_container_width=True)

if search_button or search_query:
    if not search_query and category == "Dowolna":
        st.warning("Wpisz frazę wyszukiwania lub wybierz kategorię, aby rozpocząć.")
    else:
        with st.spinner("Odpytuję API w poszukiwaniu przetargów..."):
            payload = {"query": search_query, "category": category, "min_score": min_score, "source_platform": source_filter}

            try:
                response = requests.post(API_URL, json=payload)
                response.raise_for_status()
                results = response.json()
            except requests.exceptions.RequestException as e:
                st.error(f"Błąd połączenia z API: {e}")
                results = []

        if not results:
            st.info("Nie znaleziono przetargów spełniających kryteria.")
        else:
            st.success(f"Znaleziono {len(results)} przetargów.")

            for item in results:
                with st.expander(f"Wynik: {item['title']} (Score: {item['score']:.2f})"):
                    col1, col2 = st.columns([2, 1])

                    with col1:
                        st.markdown("**Podsumowanie i treść:**")
                        content = item["content_preview"]
                        st.text(content + ("..." if len(content) >= 1000 else ""))

                    with col2:
                        st.markdown("**Szczegóły z API:**")
                        st.write(f"- **ID Przetargu:** {item['offer_id']}")
                        st.write(f"- **Platforma źródłowa:** {item['original_platform']}")
                        st.write(f"- **Typ dokumentu:** {item['source_type']}")
                        st.write(f"- **Relevance Score:** {item['score']:.2f}")
                        st.write(f"Zapisano z pliku: `{item['raw_source_path']}`")
