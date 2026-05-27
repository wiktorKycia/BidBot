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
DOCUMENT_CHUNK_SIZE = 1000
CHUNK_OVERLAP = 100

LOADERS = {
    ".pdf": PyPDFLoader,
    ".docx": UnstructuredWordDocumentLoader,
    ".doc": UnstructuredWordDocumentLoader,
    ".docm": UnstructuredWordDocumentLoader,
    ".xlsx": UnstructuredExcelLoader,
    ".xls": UnstructuredExcelLoader,
    ".xml": UnstructuredXMLLoader,
}

setup_logging()
logger = logging.getLogger("vector_db")


def convert_file(filepath: Path) -> list[Document]:
    """
    Load and chunk a file into LangChain Documents.

    Supports PDF, Word documents (.docx, .doc, .docm), Excel spreadsheets (.xlsx, .xls),
    and XML files. Splits content into chunks of 1000 characters with 100-character overlap.

    Args:
        filepath: Path to the file to convert.

    Returns:
        A list of LangChain Document objects, each with seq_num metadata indicating chunk order.
        Returns empty list if file format is unsupported or loading fails.
    """
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

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=DOCUMENT_CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
    docs = text_splitter.split_documents(docs)
    for i, doc in enumerate(docs):
        doc.metadata["seq_num"] = i + 1
    return docs


def create_loader(filepath: Path):
    """
    Create an appropriate document loader based on file extension.

    Determines which LangChain loader to use based on the file's suffix.

    Args:
        filepath: Path object indicating the file to load.

    Returns:
        A LangChain document loader instance appropriate for the file type,
        or None if the file format is not supported.
    """
    loader_cls = LOADERS.get(filepath.suffix)
    return loader_cls(str(filepath)) if loader_cls else None


async def extend_document(document: Document) -> list[Document]:
    """
    Augment a JSON document with associated attachment files and metadata.

    Reads the referenced offer JSON file to extract offer ID, title, and other metadata.
    Scans the attachments directory for the offer and converts any attachment files
    (PDFs, documents, etc.) into separate Document objects. Adds offer metadata to all documents.

    Args:
        document: A LangChain Document with a 'source' metadata field pointing to the JSON file.

    Returns:
        A list containing the original document plus all converted attachment documents,
        each with fully populated metadata (offer_id, source_type, title).
    """
    offer = await read_json(document.metadata["source"])
    offer_id = offer["id"]
    document.metadata["offer_id"] = offer_id
    document.metadata["source_type"] = "json"
    document.metadata["title"] = offer["title"]
    logger.debug(f"json Metadata:  {document.metadata}")
    logger.debug(f"attachments count: {len(offer['scraper_attachments'])}")

    attachment_documents: list[Document] = []
    offer_attachments_dir = ATTACHMENTS_DIR / offer_id
    if offer_attachments_dir.exists() and offer_attachments_dir.is_dir():
        convert_tasks = []
        for path in offer_attachments_dir.rglob("*"):
            if path.is_file():
                convert_tasks.append(asyncio.to_thread(convert_file, path))
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
    """
    Add documents to the Chroma vector store in batches.

    Processes documents in batches (MAX_CHROMA_BATCH size) to avoid exceeding
    SQL variable limits. Logs progress after each batch.

    Args:
        documents: List of LangChain Document objects to add to the vector store.
        vector_store: The Chroma vector store instance to add documents to.
    """
    for i in range(0, len(documents), MAX_CHROMA_BATCH):
        batch = documents[i : i + MAX_CHROMA_BATCH]
        vector_store.add_documents(batch)
        logger.info(f"Added batch of {len(batch)} documents to vector store. (Total processed: {i + len(batch)}/{len(documents)})")


def load_json_docs_from_directory(dirpath: Path) -> list[Document]:
    """
    Load all JSON documents from a directory using DirectoryLoader.

    Recursively scans the directory for .json files and loads them as LangChain Documents.
    Uses multithreading for parallel loading.

    Args:
        dirpath: Path to the directory containing JSON files to load.

    Returns:
        A list of LangChain Document objects loaded from all JSON files.
    """
    loader = DirectoryLoader(
        str(dirpath),
        glob="**/*.json",
        loader_cls=JSONLoader,  # type: ignore[arg-type]
        loader_kwargs={"jq_schema": ".", "text_content": False},
        use_multithreading=True,
    )
    return loader.load()


def extend_and_save_documents(vector_store: Chroma, documents: list[Document]) -> list[Document]:
    """
    Augment documents with metadata and attachment files, then save to vector store.

    Runs extend_document on each document asynchronously to attach offer metadata and
    attachment files. Adds all extended documents to the vector store in batches.

    Args:
        vector_store: The Chroma vector store to save documents to.
        documents: List of base LangChain Document objects to extend and save.

    Returns:
        A list of all extended documents that were added to the vector store.
    """

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


def load_data(vector_store: Chroma, load_data_strategy: LoadDataStrategy = LoadDataStrategy.OldDataOnly) -> list[Document]:
    """
    Load documents into the vector store according to the specified strategy.

    Supports three strategies:
    - ReloadAll: Clear existing documents and reload everything from the parsed directory.
    - AddNew: Load only new documents not already in the vector store and append to existing data.
    - OldDataOnly: Return existing documents without adding new ones.

    Retrieves existing documents from the vector store in batches to prevent SQL limits.

    Args:
        vector_store: The Chroma vector store to load into.
        load_data_strategy: Strategy for loading documents (default: OldDataOnly).

    Returns:
        A list of all documents in the system (newly added and/or existing).

    Raises:
        ValueError: If the load_data_strategy is not one of the defined strategies.
    """
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
            documents = list(new_docs)
            for doc, meta in zip(existing_documents, existing_metadatas, strict=True):
                documents.append(Document(page_content=doc, metadata=meta))

        case LoadDataStrategy.OldDataOnly:
            logger.info(f"loading documents from vector store count={len(existing_ids)}")
            for doc, meta in zip(existing_documents, existing_metadatas, strict=True):
                documents.append(Document(page_content=doc, metadata=meta))

        case _:
            raise ValueError(f"Unexpected loading data strategy: {load_data_strategy}")

    return documents
