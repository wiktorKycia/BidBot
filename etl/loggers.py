import logging.config
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = BASE_DIR / "logs"

LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
        "console": {
            "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            "datefmt": "%H:%M:%S",
        },
        "detailed": {
            "format": "%(asctime)s [%(levelname)s] %(name)s:%(funcName)s:%(lineno)d - %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "console",
            "level": "INFO",
            "stream": "ext://sys.stdout",
        },
        "file_app": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "app.log",
            "maxBytes": 5 * 1024 * 1024,  # 5 MB
            "backupCount": 3,
            "formatter": "detailed",
            "level": "DEBUG",
            "encoding": "utf-8",
        },
        "file_vector_db": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "vector_db.log",
            "maxBytes": 5 * 1024 * 1024,  # 5 MB
            "backupCount": 3,
            "formatter": "detailed",
            "level": "DEBUG",
            "encoding": "utf-8",
        },
        "file_scraper": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "scraper.log",
            "maxBytes": 5 * 1024 * 1024,  # 5 MB
            "backupCount": 3,
            "formatter": "detailed",
            "level": "DEBUG",
            "encoding": "utf-8",
        },
    },
    "loggers": {
        "": {
            "handlers": ["console", "file_app"],
            "level": "DEBUG",
            "propagate": True,
        },
        "vector_db": {
            "handlers": ["console", "file_vector_db"],
            "level": "DEBUG",
            "propagate": False,  # Prevents doubling up in app.log
        },
        "scraper": {  # Create an explicit scraper scope
            "handlers": ["console", "file_scraper"],
            "level": "DEBUG",
            "propagate": False,
        },
        "aiohttp": {"level": "WARNING"},
        "httpx": {"level": "WARNING"},
        "langchain": {"level": "WARNING"},
    },
}


DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://platformazakupowa.pl/",
}


def setup_logging():
    logging.config.dictConfig(LOGGING_CONFIG)


if not LOG_DIR.exists():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
