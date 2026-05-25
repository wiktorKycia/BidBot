import asyncio
import logging
from pathlib import Path

from langchain_chroma import Chroma
from langchain_community.document_loaders import JSONLoader, PyPDFLoader, UnstructuredExcelLoader, UnstructuredWordDocumentLoader, UnstructuredXMLLoader
from langchain_community.document_loaders.directory import DirectoryLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from etl.loggers import setup_logging
from etl.scrapers.settings import ATTACHMENTS_DIR, PARSED_DIR
from etl.utils import read_json
from etl.vector_db.models import LoadDataStrategy

MAX_CHROMA_BATCH = 5461

setup_logging()
logger = logging.getLogger("vector_db")


def convert_file(filepath: Path) -> list[Document]:
    logger.info(f"converting attachment: {filepath}")
    loader = create_loader(filepath)
    if not loader:
        logger.warning(f"Unsupported file suffix for: {filepath}")
        return []

    try:
        docs = loader.load()
    except Exception as e:
        logger.error(f"Error loading {filepath}: {e}")
        return []

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    docs = text_splitter.split_documents(docs)
    for i, doc in enumerate(docs):
        doc.metadata["seq_num"] = i + 1
    return docs


def create_loader(filepath: Path):
    if filepath.suffix == ".pdf":
        return PyPDFLoader(str(filepath))
    elif filepath.suffix in [".docx", ".doc", ".docm"]:
        return UnstructuredWordDocumentLoader(str(filepath))
    elif filepath.suffix in [".xlsx", ".xls"]:
        return UnstructuredExcelLoader(str(filepath))
    elif filepath.suffix == ".xml":
        return UnstructuredXMLLoader(str(filepath))
    return None


async def extend_document(document: Document) -> list[Document]:
    offer = await read_json(document.metadata["source"])
    offer_id = offer["id"]
    document.metadata["offer_id"] = offer_id
    document.metadata["source_type"] = "json"
    document.metadata["title"] = offer["title"]
    print(f"json Metadata:  {document.metadata}")
    # print(f"json Content:  {document.page_content}")
    print(f"attachments count: {len(offer['scraper_attachments'])}")

    attachments_list = offer["scraper_attachments"]
    attachment_documents: list[Document] = []
    if attachments_list:
        convert_tasks = []
        for attachment in attachments_list:
            if attachment["downloaded"]:
                full_path = ATTACHMENTS_DIR / offer_id / attachment["filename"]
                convert_tasks.append(asyncio.to_thread(convert_file, full_path))
        if convert_tasks:
            results = await asyncio.gather(*convert_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.exception(f"Error converting document: {res!r}")
                else:
                    attachment_documents.extend(res)

    for doc in attachment_documents:
        doc.metadata["offer_id"] = offer_id
        doc.metadata["source_type"] = "attachment"
        doc.metadata["title"] = offer["title"]

    return [document, *attachment_documents]


def add_documents_to_vector_store(documents: list[Document], vector_store: Chroma):
    for i in range(0, len(documents), MAX_CHROMA_BATCH):
        batch = documents[i : i + MAX_CHROMA_BATCH]
        vector_store.add_documents(batch)
        logger.info(f"Added {(i + 1) * MAX_CHROMA_BATCH} documents to vector store")


def load_json_docs_from_directory(dirpath: Path) -> list[Document]:
    loader = DirectoryLoader(
        str(dirpath),
        glob="**/*.json",
        loader_cls=JSONLoader,  # type: ignore[arg-type]
        loader_kwargs={"jq_schema": ".", "text_content": False},
        use_multithreading=True,
    )
    return loader.load()


def extend_and_save_documents(vector_store: Chroma, documents: list[Document]) -> list[Document]:
    async def process_documents(docs):
        tasks = [extend_document(document) for document in docs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        extended_documents = []
        for res in results:
            if isinstance(res, Exception):
                logger.exception(f"Error while extending document: {res!r}")
            else:
                extended_documents.extend(res)

        add_documents_to_vector_store(extended_documents, vector_store)
        return extended_documents

    extended_documents = asyncio.run(process_documents(documents))
    logger.info(f"added documents to vector store count={len(extended_documents)}")
    return extended_documents


def load_data(vector_store: Chroma, load_data_strategy: LoadDataStrategy = 1) -> list[Document]:
    # Instead of pulling all existing data at once which can cause "too many SQL variables", we batch it
    total_count = vector_store._collection.count()
    existing_ids = []
    existing_metadatas = []
    existing_documents = []

    for offset in range(0, total_count, MAX_CHROMA_BATCH):
        batch = vector_store.get(limit=MAX_CHROMA_BATCH, offset=offset)
        existing_ids.extend(batch.get("ids", []))
        if batch.get("metadatas"):
            existing_metadatas.extend(batch["metadatas"])
        if batch.get("documents"):
            existing_documents.extend(batch["documents"])

    documents = []

    if len(existing_ids) == 0:
        load_data_strategy = LoadDataStrategy.ReloadAll

    match load_data_strategy:
        case LoadDataStrategy.ReloadAll:
            # clear collection before adding documents in batches to avoid SQL limit
            if len(existing_ids) > 0:
                logger.info(f"clearing existing vector store ids count={len(existing_ids)}")
                for i in range(0, len(existing_ids), MAX_CHROMA_BATCH):
                    vector_store.delete(existing_ids[i : i + MAX_CHROMA_BATCH])

            documents = load_json_docs_from_directory(PARSED_DIR)
            logger.info(f"loaded documents from parsed_json_path={PARSED_DIR} count={len(documents)}")

            documents = extend_and_save_documents(vector_store, documents)

        case LoadDataStrategy.AddNew:
            documents = load_json_docs_from_directory(PARSED_DIR)
            logger.info(f"loaded documents from parsed_json_path={PARSED_DIR} count={len(documents)}")

            existing_sources = {meta.get("source") for meta in existing_metadatas if meta}
            documents = [doc for doc in documents if doc.metadata["source"] not in existing_sources]
            logger.info(f"new documents prepared to load, count={len(documents)}")

            new_docs = extend_and_save_documents(vector_store, documents)

            # Append existing old documents so the total list returned contains both
            for doc, meta in zip(existing_documents, existing_metadatas, strict=True):
                documents.append(Document(page_content=doc, metadata=meta))
            documents = new_docs

        case LoadDataStrategy.OldDataOnly:
            logger.info(f"loading documents from vector store count={len(existing_ids)}")
            for doc, meta in zip(existing_documents, existing_metadatas, strict=True):
                documents.append(Document(page_content=doc, metadata=meta))

        case _:
            raise ValueError(f"Unexpected loading data strategy: {load_data_strategy}")

    return documents
