from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw_html"
PARSED_DIR = DATA_DIR / "parsed"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"
MAX_ATTACHMENTS = 15
MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB - max rozmiar pobieranego pliku
MAX_UNCOMPRESSED_SIZE = 250 * 1024 * 1024  # 250 MB - max waga po rozpakowaniu
MAX_ZIP_FILES = 1000  # Max ilość plików wewnątrz zipa
MAX_ZIP_RATIO = 100  # Max stosunek rozmiaru rozpakowanego do oryginalnego

for d in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR]:
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://platformazakupowa.pl/",
}
