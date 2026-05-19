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
from etl.vector_db.models import IndexedDocument, RetrievalPlan
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


async def extend_offer_data(document: Document) -> list[Document]:
    offer = await read_json(document.metadata["source"])
    offer_id = offer["id"]
    document.metadata["offer_id"] = offer_id
    document.metadata["source_type"] = "json"  # the other one is attachment
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


async def add_metadata(document: Document) -> Document:
    offer = await read_json(document.metadata["source"])
    document.metadata["offer_id"] = offer["id"]
    document.metadata["source_type"] = "json"   # the other one is attachment
    document.metadata["title"] = offer["title"]
    return document


def add_documents_to_vector_store(documents: list[Document], vector_store: Chroma):
    for i in range(0, len(documents), MAX_CHROMA_BATCH):
        batch = documents[i : i + MAX_CHROMA_BATCH]
        vector_store.add_documents(batch)


def load_data(vector_store: Chroma, fresh_data_reload: bool = False):
    existing_data = vector_store.get()
    existing_ids = existing_data["ids"]
    documents = []

    if fresh_data_reload or len(existing_ids) == 0:
        loader = DirectoryLoader(str(PARSED_DIR), glob="**/*.json", loader_cls=JSONLoader, # type: ignore[arg-type]
                                 loader_kwargs={"jq_schema": ".", "text_content": False})

        documents = loader.load()
        logger.info(f"loaded documents from parsed_json_path={PARSED_DIR} count={len(documents)}")

        # clear collection before adding documents
        if len(existing_ids) > 0:
            logger.info(f"clearing existing vector store ids count={len(existing_ids)}")
            vector_store.delete(existing_ids)

        async def apply_metadata(docs):
            tasks = [extend_offer_data(document) for document in docs]
            return await asyncio.gather(*tasks, return_exceptions=True)

        results = asyncio.run(apply_metadata(documents))

        documents = []

        for res in results:
            if isinstance(res, Exception):
                logger.exception(f"Error while adding metadata to document: {res!r}")
            else:
                documents.append(res)

        add_documents_to_vector_store(documents, vector_store)
        logger.info(f"added documents to vector store count={len(documents)}")
    else:
        logger.info(f"loading documents from vector store count={len(existing_ids)}")
        for doc, meta in zip(existing_data["documents"], existing_data["metadatas"], strict=True):
            documents.append(Document(page_content=doc, metadata=meta))
