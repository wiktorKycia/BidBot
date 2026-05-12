import os
from dotenv import load_dotenv

load_dotenv("../.env")
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]

MODEL = "gpt-4o-mini"
MODEL_EMBEDDINGS = "text-embedding-3-large"

from langchain_openai import OpenAIEmbeddings
from langchain_chroma import Chroma

CHROMA_DB_PATH = "./chroma_langchain_db"
embeddings = OpenAIEmbeddings(model=MODEL_EMBEDDINGS)

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

from langchain_community.document_loaders import JSONLoader, DirectoryLoader
import json
from pathlib import Path

folder_path = Path(__file__).resolve().parent.parent / "etl" / "scrapers" / "platformazakupowa2" / "data" / "parsed"
file_path = "./example_data.json"
data = json.loads(Path(file_path).read_text())

loader = DirectoryLoader(folder_path, glob="**/*.json", loader_cls=JSONLoader, loader_kwargs = {'jq_schema':'.', 'text_content': False})

# loader = JSONLoader(file_path=file_path, jq_schema='.', text_content=False)
documents = loader.load()

# clear collection before adding documents
ids = vector_store.get()["ids"]
if len(ids) > 0:
    vector_store.delete(ids)

vector_store.add_documents(documents)

from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

def format_docs(docs):
    return "\n\n".join(doc.page_content for doc in docs)

llm = ChatOpenAI(model=MODEL)
prompt = ChatPromptTemplate.from_messages(
    [
        ("system", """You are a helpful assistant and a literature specialist. Answer all questions to the best of your ability.
        During answering use this context from documents: {context}. If you do not know the answer - do not provide it.
        Answer as short as possible, preferably in one sentence"""),
        ("human", "{input}"),
    ]
)

retriever = vector_store.as_retriever()

rag_chain_from_docs = (
    {
        "context": retriever | RunnableLambda(format_docs),
        "input": RunnablePassthrough(),
    }
    | RunnableLambda(lambda values: prompt.format_messages(**values))
    | llm
)


def ask(question: str) -> str:
    message = ""
    for response in rag_chain_from_docs.stream(question):
        message += response.content
    return message


def main():
    print("Console RAG chat ready. Type your question, or 'exit' to quit.")
    while True:
        try:
            contents = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            break

        if not contents:
            continue
        if contents.lower() in {"exit", "quit"}:
            print("Bye.")
            break

        try:
            answer = ask(contents)
            print(f"Assistant: {answer}")
        except Exception as e:
            print(f"Error: {e}")


if __name__ == "__main__":
    main()
