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
            for next_msg in history_messages[idx + 1:]:
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
