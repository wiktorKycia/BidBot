import logging.config
from pathlib import Path

LOG_DIR = Path("logs")

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
        "rotatingFile": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": LOG_DIR / "vector_db.log",
            "maxBytes": 5000000,
            "backupCount": 1,
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
        "httpx": {"level": "WARNING"},
        "vector_db": {
            "handlers": ["rotatingFile"],
            "level": "DEBUG",
            "propagate": False,
        },
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
