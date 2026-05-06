import asyncio
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone, timedelta

import aiofiles
import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

BASE_URL = "https://platformaofertowa.pl"
API_LIST_URL = "https://api.platformaofertowa.pl/tenders/search/best-match"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PARSED_DIR = DATA_DIR / "parsed"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
LAST_RUN_FILE = DATA_DIR / "last_run.txt"

SEMAPHORE = asyncio.Semaphore(30)


def setup_directories():
    for directory in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def get_last_run_date() -> datetime:
    if LAST_RUN_FILE.exists():
        try:
            date_str = LAST_RUN_FILE.read_text().strip()
            return datetime.fromisoformat(date_str)
        except ValueError:
            pass
    return datetime(2000, 1, 1, tzinfo=timezone.utc)


def set_last_run_date(dt: datetime):
    LAST_RUN_FILE.write_text(dt.isoformat())


async def save_text(filepath: Path, data: str):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(data)


async def save_json(filepath: Path, data: dict):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))


async def download_file(session: aiohttp.ClientSession, url: str, save_dir: Path) -> dict:
    if not url.startswith("http"):
        url = f"{BASE_URL}{url}" if url.startswith("/") else f"{BASE_URL}/{url}"

    filename = url.split('/')[-1].split('?')[0]
    if not filename or filename.endswith("/"):
        filename = f"zalacznik_{hashlib.md5(url.encode()).hexdigest()[:6]}.pdf"

    filepath = save_dir / filename
    metadata = {
        "url": url,
        "filename": filename,
        "size_bytes": 0,
        "downloaded": False
    }

    if filepath.exists():
        metadata["size_bytes"] = filepath.stat().st_size
        metadata["downloaded"] = True
        return metadata

    async with SEMAPHORE:
        try:
            async with session.get(url) as response:
                if response.status == 200:
                    size = 0
                    async with aiofiles.open(filepath, mode='wb') as f:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            await f.write(chunk)
                            size += len(chunk)

                    metadata["size_bytes"] = size
                    metadata["downloaded"] = True
                    print(f"    Pobrano plik: {filename}")
        except Exception as e:
            print(f"    Błąd pobierania {url}: {e}")

    return metadata


async def check_page_exists(session: aiohttp.ClientSession, page: int) -> bool:
    limit = 50
    offset = (page - 1) * limit
    deadline_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    payload = {
        "query": "",
        "filters": {
            "cpv_codes": None,
            "locality": {"locality": None},
            "submitting_offers_deadline_from": deadline_from
        },
        "limit": 1,
        "offset": offset,
        "active_status": 1,
        "search_config": {"reranking_limit": 1000},
        "sort_by": "submitting_offers_deadline_asc"
    }

    headers = {
        "Host": "api.platformaofertowa.pl",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://platformaofertowa.pl",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    }

    try:
        async with session.post(API_LIST_URL, json=payload, headers=headers) as response:
            if response.status == 200:
                api_data = await response.json()
                return len(api_data.get("tenders", [])) > 0
    except Exception:
        pass
    return False


async def get_total_pages(session: aiohttp.ClientSession) -> int:
    low = 1
    high = 5

    while await check_page_exists(session, high):
        low = high
        high *= 2

    last_valid = low
    while low <= high:
        mid = (low + high) // 2

        if await check_page_exists(session, mid):
            last_valid = mid
            low = mid + 1
        else:
            high = mid - 1

    print(f"Liczba stron do pobrania: {last_valid}")
    return last_valid


async def process_single_notice(session: aiohttp.ClientSession, item: dict) -> dict:
    notice_id = str(item.get("id"))

    raw_html_desc = item.get("description")
    if raw_html_desc is None:
        raw_html_desc = ""

    doc = BeautifulSoup(raw_html_desc, "lxml")
    clean_description = doc.get_text(separator="\n", strip=True)

    await save_text(RAW_DIR / f"{notice_id}.html", raw_html_desc)

    attachment_links = set()
    for a_tag in doc.find_all("a", href=True):
        href = str(a_tag['href'])
        href_lower = href.lower()
        if any(ext in href_lower for ext in [".pdf", ".zip", ".doc", ".docx", "download", "file"]):
            attachment_links.add(href)

    tender_attachments_dir = ATTACHMENTS_DIR / notice_id
    if attachment_links:
        tender_attachments_dir.mkdir(parents=True, exist_ok=True)

    download_tasks = [download_file(session, link, tender_attachments_dir) for link in attachment_links]
    attachments_metadata = await asyncio.gather(*download_tasks) if download_tasks else []

    parsed_data = {
        **item,
        "scraper_url": f"{BASE_URL}/pl/tenders/tenders-list/{notice_id}",
        "scraper_clean_description": clean_description,
        "scraper_attachments": attachments_metadata
    }

    return parsed_data


async def process_list_page(session: aiohttp.ClientSession, page: int, last_run_date: datetime) -> int:
    print(f"\nSprawdzanie strony {page}")

    limit = 50
    offset = (page - 1) * limit
    deadline_from = (datetime.now(timezone.utc) - timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S.000Z')

    payload = {
        "query": "",
        "filters": {
            "cpv_codes": None,
            "locality": {"locality": None},
            "submitting_offers_deadline_from": deadline_from
        },
        "limit": limit,
        "offset": offset,
        "active_status": 1,
        "search_config": {"reranking_limit": 1000},
        "sort_by": "submitting_offers_deadline_asc"
    }

    headers = {
        "Host": "api.platformaofertowa.pl",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://platformaofertowa.pl",
        "Referer": "https://platformaofertowa.pl/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    async with SEMAPHORE:
        try:
            async with session.post(API_LIST_URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    print(f"Błąd HTTP {response.status} podczas pobierania strony {page}")
                    return 0
                api_data = await response.json()
        except Exception as e:
            print(f"Błąd połączenia z API: {e}")
            return 0

    tenders = api_data.get("tenders", [])

    if not tenders:
        print("Koniec wyników.")
        return 0

    tasks = []

    for item in tenders:
        raw_pub_date = item.get("publicationDate")

        if raw_pub_date:
            try:
                pub_date = datetime.fromisoformat(str(raw_pub_date).replace('Z', '+00:00'))
                if pub_date <= last_run_date:
                    print(f"Trafiono na stare ogłoszenie ({pub_date.strftime('%Y-%m-%d %H:%M')}). Reszta skip")
                    break
            except (ValueError, TypeError):
                pass

        tasks.append(process_single_notice(session, item))

    results = await asyncio.gather(*tasks) if tasks else []

    saved_count = 0
    for res in results:
        if res:
            await save_json(PARSED_DIR / f"{res['id']}.json", res)
            saved_count += 1

    return saved_count


async def main():
    setup_directories()

    last_run_date = get_last_run_date()
    current_run_date = datetime.now(timezone.utc)
    print(f"Ostatnie uruchomienie: {last_run_date.strftime('%Y-%m-%d %H:%M:%S')}")

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    timeout = aiohttp.ClientTimeout(total=45)

    total_downloaded = 0

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            total_pages = await get_total_pages(session)

            print(f"Pobieranie {total_pages} stron...")
            tasks = [process_list_page(session, page, last_run_date) for page in range(1, total_pages + 1)]

            results = await asyncio.gather(*tasks)
            total_downloaded = sum(results)

    finally:
        set_last_run_date(current_run_date)
        print(f"\nZakończono pracę programu. Łącznie pobrano {total_downloaded} nowych przetargów.")


if __name__ == "__main__":
    asyncio.run(main())
