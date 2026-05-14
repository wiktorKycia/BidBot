import asyncio
import email.message
import hashlib
import logging
import mimetypes
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import aiofiles
import aiohttp
import backoff
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

from etl.settings import ATTACHMENTS_DIR, BASE_DIR, LAST_RUN_FILE, PARSED_DIR, RAW_DIR, setup_logging
from etl.utils import save_json, save_to_file

setup_logging()
logger = logging.getLogger(__name__)

BASE_URL = "https://platformaofertowa.pl"
API_LIST_URL = "https://api.platformaofertowa.pl/tenders/search/best-match"


def setup_directories():
    for directory in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def get_last_run_date() -> datetime:
    if LAST_RUN_FILE.exists():
        try:
            date_str = LAST_RUN_FILE.read_text().strip()
            return datetime.fromisoformat(date_str)
        except ValueError:
            logger.warning("Niepoprawny format daty w last_run.txt. Przyjęto datę domyślną.")
    return datetime(2000, 1, 1, tzinfo=UTC)


def set_last_run_date(dt: datetime):
    LAST_RUN_FILE.write_text(dt.isoformat())


def extract_direct_url(url: str) -> str:
    parsed = urlparse(url)

    if "google.com" in parsed.netloc and any(path_part in parsed.path for path_part in ["/viewer", "/gview"]):
        query_params = parse_qs(parsed.query)
        if "url" in query_params:
            return unquote(query_params["url"][0])
        elif "src" in query_params:
            return unquote(query_params["src"][0])

    return url


def extract_best_url(item: dict) -> str:
    extra_data = item.get("sourceExtraData") or {}

    integrations_tender = extra_data.get("integrationsTender") or {}
    web_source = integrations_tender.get("webSourceUrl")
    if web_source:
        return web_source

    integrations_notices = extra_data.get("integrationsNotices") or []
    if isinstance(integrations_notices, list) and len(integrations_notices) > 0:
        notice_url = integrations_notices[0].get("webSourceUrl")
        if notice_url:
            return notice_url

    website_url = item.get("websiteUrl") or ""
    if website_url and "transakcja" in website_url:
        return website_url

    origin_urls = item.get("originUrls") or []
    if isinstance(origin_urls, list):
        for url_candidate in origin_urls:
            url_candidate = str(url_candidate).strip()
            if url_candidate.startswith("http") and " " not in url_candidate:
                return url_candidate

    if website_url and str(website_url).startswith("http"):
        return website_url

    source_url = item.get("sourceUrl") or ""
    if source_url and str(source_url).startswith("http"):
        return source_url

    notice_id = str(item.get("id"))
    return f"{BASE_URL}/pl/tenders/tenders-list/{notice_id}"


def is_fatal_error(e):
    if isinstance(e, aiohttp.ClientResponseError):
        return e.status != 429 and e.status < 500
    return False


@backoff.on_exception(
    backoff.expo,
    (aiohttp.ClientError, asyncio.TimeoutError),
    max_tries=3,
    giveup=is_fatal_error,
    logger=logger,
)
async def fetch_html(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> str:
    async with semaphore:
        domain = urlparse(url).netloc
        if "ted.europa.eu" in domain:
            await asyncio.sleep(2.0)
        elif "platformazakupowa.pl" in domain:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.5)

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "pl,en-US;q=0.7,en;q=0.3",
        }
        async with session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.text()


@backoff.on_exception(
    backoff.expo,
    (aiohttp.ClientError, asyncio.TimeoutError),
    max_tries=3,
    giveup=is_fatal_error,
    logger=logger,
)
async def download_file(
    session: aiohttp.ClientSession,
    url: str,
    save_dir: Path,
    semaphore: asyncio.Semaphore,
) -> dict:
    url = extract_direct_url(url)
    url = urljoin(BASE_URL, url)

    filename = url.split("/")[-1].split("?")[0]
    valid_extensions = [".pdf", ".zip", ".7z", ".rar", ".doc", ".docx", ".xls", ".xlsx"]
    has_valid_extension = any(filename.lower().endswith(ext) for ext in valid_extensions)

    if not filename or not has_valid_extension:
        filename = f"zalacznik_{hashlib.md5(url.encode()).hexdigest()[:6]}"

    filepath = save_dir / filename

    metadata = {
        "url": url,
        "filename": filename,
        "local_path": str(filepath.relative_to(BASE_DIR)) if filepath else "",
        "size_bytes": 0,
        "downloaded": False,
    }

    async with semaphore, session.get(url) as response:
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            logger.warning(f"Ominięto plik, ponieważ URL zwraca stronę HTML: {url}")
            metadata["downloaded"] = False
            return metadata

        cd_header = response.headers.get("Content-Disposition")
        if cd_header:
            msg = email.message.Message()
            msg["Content-Disposition"] = cd_header
            cd_filename = msg.get_filename()
            if cd_filename:
                filename = cd_filename
                filepath = save_dir / filename

        if "." not in filename:
            content_type = response.headers.get("Content-Type", "")
            ext = mimetypes.guess_extension(content_type) or ".bin"
            filename += ext
            filepath = save_dir / filename

        metadata["filename"] = filename
        metadata["local_path"] = str(filepath.relative_to(BASE_DIR))

        if filepath.exists():
            metadata["size_bytes"] = filepath.stat().st_size
            metadata["downloaded"] = True
            return metadata

        temp_filepath = filepath.with_suffix(".tmp")
        size = 0

        async with aiofiles.open(temp_filepath, mode="wb") as f:
            async for chunk in response.content.iter_chunked(64 * 1024):
                if chunk:
                    await f.write(chunk)
                    size += len(chunk)

        temp_filepath.rename(filepath)

        metadata["size_bytes"] = size
        metadata["downloaded"] = True
        logger.info(f"Pobrano plik: {filename} ({size} bytes)")

        return metadata


async def check_page_exists(session: aiohttp.ClientSession, page: int) -> bool:
    limit = 50
    offset = (page - 1) * limit
    deadline_from = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "query": "",
        "filters": {
            "cpv_codes": None,
            "locality": {"locality": None},
            "submitting_offers_deadline_from": deadline_from,
        },
        "limit": 1,
        "offset": offset,
        "active_status": 1,
        "search_config": {"reranking_limit": 1000},
        "sort_by": "submitting_offers_deadline_asc",
    }

    headers = {
        "Host": "api.platformaofertowa.pl",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://platformaofertowa.pl",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    try:
        async with session.post(API_LIST_URL, json=payload, headers=headers) as response:
            if response.status == 200:
                api_data = await response.json()
                return len(api_data.get("tenders", [])) > 0
    except Exception as e:
        logger.debug(f"Błąd przy sprawdzaniu strony {page}: {e}")
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

    logger.info(f"Liczba stron do pobrania: {last_valid}")
    return last_valid


async def process_single_notice(
    session: aiohttp.ClientSession,
    item: dict,
    html_semaphore: asyncio.Semaphore,
    file_semaphore: asyncio.Semaphore,
) -> dict:
    notice_id = str(item.get("id"))
    original_url = extract_best_url(item)

    raw_html = ""
    try:
        raw_html = await fetch_html(session, original_url, html_semaphore)
    except Exception as e:
        logger.error("==== BŁĄD POBIERANIA ====")
        logger.error(f"ID ogłoszenia: {notice_id}")
        logger.error(f"Nie udało się pobrać strony ogłoszenia {original_url}: {e!r}")
        logger.error("=========================")

    await save_to_file(RAW_DIR / f"{notice_id}.html", raw_html)

    doc = BeautifulSoup(raw_html, "lxml")
    clean_description = doc.get_text(separator="\n", strip=True)

    attachment_links = set()
    valid_link_keywords = [".pdf", ".zip", ".7z", ".rar", ".doc", ".docx", ".xls", ".xlsx", "download", "file", "viewer"]

    for a_tag in doc.find_all("a", href=True):
        href = str(a_tag["href"])
        href_lower = href.lower()

        if any(ext in href_lower for ext in valid_link_keywords):
            full_link = urljoin(original_url, href)
            attachment_links.add(full_link)

    tender_attachments_dir = ATTACHMENTS_DIR / notice_id
    if attachment_links:
        tender_attachments_dir.mkdir(parents=True, exist_ok=True)

    download_tasks = [download_file(session, link, tender_attachments_dir, file_semaphore) for link in attachment_links]

    valid_attachments_metadata = []
    if download_tasks:
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Wyjątek w tasku pobierania dla notice {notice_id}: {res!r}")
            elif res and res.get("downloaded"):
                valid_attachments_metadata.append(res)

    parsed_data = {
        **item,
        "scraper_source_url": original_url,
        "scraper_clean_description": clean_description[:2000],
        "scraper_attachments": valid_attachments_metadata,
    }

    return parsed_data


async def process_list_page(
    session: aiohttp.ClientSession,
    page: int,
    last_run_date: datetime,
    html_semaphore: asyncio.Semaphore,
    file_semaphore: asyncio.Semaphore,
) -> int:
    logger.info(f"Przetwarzanie strony {page}")

    limit = 50
    offset = (page - 1) * limit
    deadline_from = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    payload = {
        "query": "",
        "filters": {
            "cpv_codes": None,
            "locality": {"locality": None},
            "submitting_offers_deadline_from": deadline_from,
        },
        "limit": limit,
        "offset": offset,
        "active_status": 1,
        "search_config": {"reranking_limit": 1000},
        "sort_by": "submitting_offers_deadline_asc",
    }

    headers = {
        "Host": "api.platformaofertowa.pl",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://platformaofertowa.pl",
        "Referer": "https://platformaofertowa.pl/",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }

    async with html_semaphore:
        try:
            async with session.post(API_LIST_URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    logger.error(f"Błąd HTTP {response.status} podczas pobierania strony {page}")
                    return 0
                api_data = await response.json()
        except Exception as e:
            logger.error(f"Błąd połączenia z API na stronie {page}: {e}")
            return 0

    tenders = api_data.get("tenders", [])
    if not tenders:
        logger.info(f"Brak wyników na stronie {page}.")
        return 0

    tasks = []
    for item in tenders:
        raw_pub_date = item.get("publicationDate")

        if raw_pub_date:
            try:
                pub_date_str = str(raw_pub_date).replace("Z", "+00:00")
                pub_date = datetime.fromisoformat(pub_date_str)
                if pub_date <= last_run_date:
                    logger.info(f"Trafiono na starsze ogłoszenie ({pub_date.strftime('%Y-%m-%d %H:%M')}). Pomijam resztę.")
                    break
            except (ValueError, TypeError) as e:
                logger.debug(f"Błąd parsowania daty: {raw_pub_date} - {e}")

        tasks.append(process_single_notice(session, item, html_semaphore, file_semaphore))

    saved_count = 0
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, Exception):
                logger.error(f"Wyjątek podczas przetwarzania ogłoszenia: {res!r}")
            elif res:
                await save_json(PARSED_DIR / f"{res['id']}.json", res)
                saved_count += 1

    return saved_count


async def main():
    setup_directories()

    last_run_date = get_last_run_date()
    current_run_date = datetime.now(UTC)
    logger.info(f"Ostatnie uruchomienie: {last_run_date.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    timeout = aiohttp.ClientTimeout(total=60)

    html_semaphore = asyncio.Semaphore(2)
    file_semaphore = asyncio.Semaphore(10)

    total_downloaded = 0

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            total_pages = await get_total_pages(session)

            logger.info(f"Rozpoczęcie pobierania {total_pages} stron...")
            tasks = [process_list_page(session, page, last_run_date, html_semaphore, file_semaphore) for page in range(1, total_pages + 1)]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for res in results:
                if isinstance(res, Exception):
                    logger.error(f"Błąd przy taskach głównych stron: {res!r}")
                else:
                    total_downloaded += res

    finally:
        set_last_run_date(current_run_date)
        logger.info(f"Zakończono pracę programu. Łącznie pobrano {total_downloaded} nowych przetargów.")


if __name__ == "__main__":
    asyncio.run(main())
