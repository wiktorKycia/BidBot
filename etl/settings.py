import logging.config
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw_html"
PARSED_DIR = DATA_DIR / "parsed"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
LOG_DIR = Path("logs")
MAX_ATTACHMENTS = 15
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB - max rozmiar pobieranego pliku
MAX_UNCOMPRESSED_SIZE = 250 * 1024 * 1024  # 250 MB - max waga po rozpakowaniu
MAX_ZIP_FILES = 1000  # Max ilość plików wewnątrz zipa
MAX_ZIP_RATIO = 100  # Max stosunek rozmiaru rozpakowanego do oryginalnego

for d in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR, LOG_DIR]:
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "level": "INFO",
            "stream": "ext://sys.stdout",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": LOG_DIR / "scraper.log",
            "formatter": "standard",
            "level": "DEBUG",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "": {
            "handlers": ["console", "file"],
            "level": "DEBUG",
            "propagate": True,
        },
        "aiohttp": {"level": "WARNING"},
    },
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)
