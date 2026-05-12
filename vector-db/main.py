import os
from dotenv import load_dotenv

load_dotenv("../.env")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

MODEL = "gpt-4o-mini"
MODEL_EMBEDDINGS = "text-embedding-3-large"

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

CHROMA_DB_PATH = "./chroma_langchain_db"
embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS, api_key=OPENAI_API_KEY)

vector_store = Chroma(
    collection_name="bid_info_json",
    embedding_function=embeddings,
    persist_directory=CHROMA_DB_PATH
)

from chromadb import PersistentClient

def delete_collection(chroma_path: str, collection_name: str):
    try:
        chroma_client = PersistentClient(path=chroma_path)
        chroma_client.delete_collection(collection_name)
        print(f"Collection {collection_name} deleted successfully.")
    except Exception as e:
        raise Exception(f"Unable to delete collection: {e}")

from langchain_community.document_loaders import JSONLoader
import json
from pathlib import Path

from langchain_community.document_loaders import JSONLoader
import json
from pathlib import Path

# file_path = Path(__file__).resolve().parent / "etl" / "scrapers" / "platformazakupowa2" / "data" / "parsed" / "435638.json"
file_path = "./example_data.json"
data = json.loads(Path(file_path).read_text())
loader = JSONLoader( file_path=file_path, jq_schema='.', text_content=False)
document = loader.load()

# from langchain_text_splitters import RecursiveJsonSplitter
#
# json_splitter = RecursiveJsonSplitter()
# print(document)

# chunked_documents = json_splitter.split_json(document[0])


# clear collection before adding documents
ids = vector_store.get()["ids"]
if len(ids) > 0: vector_store.delete(ids)

vector_store.add_documents(document)


from langchain_core.chat_history import (
    BaseChatMessageHistory,
    InMemoryChatMessageHistory,
)

STORE = {}

def get_session_history(session_id: str) -> BaseChatMessageHistory:
    if session_id not in STORE:
        STORE[session_id] = InMemoryChatMessageHistory()
    return STORE[session_id]


from langchain_core.runnables import RunnableParallel
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_openai import ChatOpenAI


if "rag_session_1" in STORE:
    del STORE["rag_session_1"]

config = {"configurable": {"session_id": "rag_session_1"}}

def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

llm = ChatOpenAI(model=MODEL, api_key=OPENAI_API_KEY)
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", """You are a helpful assistant and a literature specialist. Answer all questions to the best of your ability.
        During answering use this context from documents: {context}. If you do not know the answer - do not provide it.
            Answear as short as possible, preferably in one sentence""",
        ),
        MessagesPlaceholder(variable_name="chat_history"),
        ("human", "{input}"),
    ]
)

chain = prompt | llm

from langchain_core.runnables.history import RunnableWithMessageHistory
from langchain_core.runnables import RunnablePassthrough

rag_chain_with_history = RunnableWithMessageHistory(
    chain,
    get_session_history,
    input_messages_key="input",
    history_messages_key="chat_history",
)

retriever = vector_store.as_retriever()
rag_chain_from_docs = (
    {
        "context": retriever | format_docs,
        "input": RunnablePassthrough(),
    }
    | rag_chain_with_history
)


import panel as pn

pn.extension()


async def callback(contents: str, user: str, instance: pn.chat.ChatInterface):
    message = ""
    for response in rag_chain_from_docs.stream(contents, config=config):
        message += response.content
        yield message


# config = {"configurable": {"session_id": "rag_session_1"}}
chat_interface = pn.chat.ChatInterface(callback=callback, callback_user="")
chat_interface.servable()