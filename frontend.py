import requests
import streamlit as st

from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI
from etl.llms import MODEL, require_openai_api_key

from api import TenderDetail

OPENAI_API_KEY = require_openai_api_key()
llm = ChatOpenAI(model=MODEL)

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

        describer = ChatOpenAI(model=MODEL, temperature=0, max_retries=3)
        
        llm_answer = ChatPromptTemplate.from_messages(
            [
                SystemMessagePromptTemplate.from_template("""You are an expert assistant for analyzing public procurement tenders.
You have access to a list of tender data objects matching the following schema:
- offer_id (str): Unique identifier of the tender
- title (str): Title of the tender
- score (float): Search relevance score
- source_url (str): URL to the original tender
- deadline (str): Deadline for submitting offers
- buyer (str): The entity that published the tender
- description (str): Extracted description and tags
- full_data (str): Raw JSON string of the complete tender data
- raw_text_preview (str): Text chunks extracted from the tender documents

Your goal is to summarize all the provided tender data and present it in a clear, user-friendly Markdown format.
Focus on the most important aspects: buyer, deadline, and a concise summary of the requirements. Group related tenders if possible, and structure the response so the user can easily evaluate the opportunities."""),
                (
                    "human",
                    "Data to summarize: {summary}",
                ),
            ]
        )
        
        # tender_details = [TenderDetail(**item) for item in results]
        message = describer.invoke(llm_answer.format_messages(summary=results)).content.strip()

        st.markdown(message)

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
