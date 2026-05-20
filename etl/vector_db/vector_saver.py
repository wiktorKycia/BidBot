'''
scan parsed JSON folder
scan attachment folders recursively
tie files to a specific offer ID
manage ingestion flow
'''
import asyncio
import json
import logging
import re
from argparse import ArgumentError
from operator import itemgetter
from pathlib import Path
from typing import Any

from chromadb import PersistentClient
from langchain_chroma import Chroma
from langchain_community.document_loaders import (
    JSONLoader,
    PyPDFLoader,
    UnstructuredWordDocumentLoader,
    UnstructuredExcelLoader,
    UnstructuredXMLLoader
)
from langchain_community.document_loaders.directory import DirectoryLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document
from langchain_core.prompts import ChatPromptTemplate, SystemMessagePromptTemplate
from langchain_openai import ChatOpenAI, OpenAIEmbeddings

from etl.llms import MODEL, require_openai_api_key
from etl.loggers import setup_logging
from etl.scrapers.settings import PARSED_DIR, ATTACHMENTS_DIR
from etl.vector_db.models import IndexedDocument, RetrievalPlan, LoadDataStrategy
from etl.vector_db.prompts import main_system_message_template, use_search_system_message_template
from etl.utils import read_json

MAX_CHROMA_BATCH = 5461

setup_logging()
logger = logging.getLogger("vector_db")


def convert_file(filepath: Path) -> list[Document]:
    loader = create_loader(filepath)
    docs = loader.load()

    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=1000,
        chunk_overlap=100
    )
    return text_splitter.split_documents(docs)

def create_loader(filepath: Path):
    if filepath.suffix == ".pdf":
        return PyPDFLoader(str(filepath))
    elif filepath.suffix in [".docx",".doc",".docm"]:
        return UnstructuredWordDocumentLoader(str(filepath))
    elif filepath.suffix in [".xlsx", ".xls"]:
        return UnstructuredExcelLoader(str(filepath))
    elif filepath.suffix == ".xml":
        return UnstructuredXMLLoader(str(filepath))
    raise Exception("unsupported file suffix")


async def extend_document(document: Document) -> list[Document]:
    offer = await read_json(document.metadata["source"])
    offer_id = offer["id"]
    document.metadata["offer_id"] = offer_id
    document.metadata["source_type"] = "json"
    document.metadata["title"] = offer["title"]

    attachments_list = offer["scraper_attachments"]
    attachment_documents: list[Document] = []
    if attachments_list:
        for attachment in attachments_list:
            if attachment["downloaded"]:
                full_path = ATTACHMENTS_DIR / offer_id / attachment["filename"]
                attachment_documents.extend(convert_file(full_path))

    for doc in attachment_documents:
        doc.metadata["offer_id"] = offer_id
        doc.metadata["source_type"] = "attachment"
        doc.metadata["title"] = offer["title"]

    return [document, *attachment_documents]


def add_documents_to_vector_store(documents: list[Document], vector_store: Chroma):
    for i in range(0, len(documents), MAX_CHROMA_BATCH):
        batch = documents[i : i + MAX_CHROMA_BATCH]
        vector_store.add_documents(batch)

def load_json_docs_from_directory(dirpath: Path) -> list[Document]:
    loader = DirectoryLoader(str(dirpath), glob="**/*.json", loader_cls=JSONLoader,  # type: ignore[arg-type]
                             loader_kwargs={"jq_schema": ".", "text_content": False})
    return loader.load()


def extend_and_save_documents(vector_store: Chroma, documents: list[Document]):
    async def extend_documents(docs):
        tasks = [extend_document(document) for document in docs]
        return await asyncio.gather(*tasks, return_exceptions=True)

    results = asyncio.run(extend_documents(documents))

    extended_documents = []

    for res in results:
        if isinstance(res, Exception):
            logger.exception(f"Error while adding metadata to document: {res!r}")
        else:
            extended_documents.append(res)

    add_documents_to_vector_store(extended_documents, vector_store)
    logger.info(f"added documents to vector store count={len(extended_documents)}")


def load_data(vector_store: Chroma, load_data_strategy: LoadDataStrategy = 1) -> list[Document]:
    existing_data = vector_store.get()
    existing_ids = existing_data["ids"]
    documents = []

    if len(existing_ids) == 0:
        load_data_strategy = LoadDataStrategy.ReloadAll

    match load_data_strategy:
        case LoadDataStrategy.ReloadAll:
            # clear collection before adding documents
            if len(existing_ids) > 0:
                logger.info(f"clearing existing vector store ids count={len(existing_ids)}")
                vector_store.delete(existing_ids)


            documents = load_json_docs_from_directory(PARSED_DIR)
            logger.info(f"loaded documents from parsed_json_path={PARSED_DIR} count={len(documents)}")

            extend_and_save_documents(vector_store, documents)


        case LoadDataStrategy.AddNew:
            documents = load_json_docs_from_directory(PARSED_DIR)
            logger.info(f"loaded documents from parsed_json_path={PARSED_DIR} count={len(documents)}")

            existing_sources = {meta.get("source") for meta in existing_data["metadatas"] if meta}
            documents = [doc for doc in documents if doc.metadata["source"] not in existing_sources]
            logger.info(f"new documents prepared to load, count={len(documents)}")

            extend_and_save_documents(vector_store, documents)


        case LoadDataStrategy.OldDataOnly:
            logger.info(f"loading documents from vector store count={len(existing_ids)}")
            for doc, meta in zip(existing_data["documents"], existing_data["metadatas"], strict=True):
                documents.append(Document(page_content=doc, metadata=meta))


        case _:
            raise ValueError(f"Unexpected loading data strategy: {load_data_strategy}")


    return documents
