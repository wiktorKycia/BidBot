import os

import requests
import streamlit as st

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
    for i in range(0, len(st.session_state.messages) - 1, 2):
        if i + 1 < len(st.session_state.messages):
            api_history.append({"user": st.session_state.messages[i]["content"], "assistant": st.session_state.messages[i + 1]["content"]})

    payload = {"message": user_input, "history": api_history}

    with st.chat_message("assistant"), st.spinner("Myślę..."):
        try:
            response = requests.post(API_URL, json=payload, timeout=30)
            response.raise_for_status()
            answer = response.json()["answer"]
            st.markdown(answer)
        except requests.exceptions.ConnectionError:
            st.error(f"❌ Błąd połączenia! Streamlit próbował uderzyć pod: **{API_URL}**")
        except requests.exceptions.HTTPError:
            st.error(f"❌ Błąd API [{response.status_code}]: {response.text}")
        except Exception as e:
            st.error(f"❌ Nieoczekiwany błąd: {e}")

    st.session_state.messages.append({"role": "assistant", "content": answer})
