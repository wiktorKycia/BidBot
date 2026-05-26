import asyncio
import email.message
import hashlib
import logging
import mimetypes
import random
import zipfile
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
    def get_tender_url(i: dict):
        return i.get("sourceExtraData", {}).get("integrationsTender", {}).get("webSourceUrl")

    def get_notice_url(i: dict):
        notices = i.get("sourceExtraData", {}).get("integrationsNotices")
        if isinstance(notices, list) and notices:
            return notices[0].get("webSourceUrl")
        return None

    def get_transaction_url(i: dict):
        url = i.get("websiteUrl") or ""
        return url if "transakcja" in url else None

    def get_origin_url(i: dict):
        urls = i.get("originUrls")
        if isinstance(urls, list):
            for u in urls:
                u_str = str(u).strip()
                if u_str.startswith("http") and " " not in u_str:
                    return u_str
        return None

    def get_website_url(i: dict):
        url = i.get("websiteUrl") or ""
        return url if str(url).startswith("http") else None

    def get_source_url(i: dict):
        url = i.get("sourceUrl") or ""
        return url if str(url).startswith("http") else None

    strategies = [
        get_tender_url,
        get_notice_url,
        get_transaction_url,
        get_origin_url,
        get_website_url,
        get_source_url,
    ]

    for strategy in strategies:
        if url := strategy(item):
            return url

    notice_id = str(item.get("id", ""))
    return f"{BASE_URL}/pl/tenders/tenders-list/{notice_id}"


def is_fatal_error(e):
    if isinstance(e, aiohttp.ClientResponseError):
        return e.status != 429 and e.status < 500
    return False


@backoff.on_exception(
    backoff.expo,
    (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError),
    max_tries=3,
    giveup=is_fatal_error,
    logger=logger,
)
async def fetch_html(session: aiohttp.ClientSession, url: str, semaphore: asyncio.Semaphore) -> str:
    async with semaphore:
        await asyncio.sleep(random.uniform(2.0, 4.0))

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
    (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError, OSError),
    max_tries=3,
    giveup=is_fatal_error,
    logger=logger,
)
async def fetch_attachments_metadata(session: aiohttp.ClientSession, notice_id: str, semaphore: asyncio.Semaphore) -> set:
    api_attachments_url = f"https://api.platformaofertowa.pl/tenders/tender/{notice_id}/attachments"

    attachments_list_headers = {
        "Host": "api.platformaofertowa.pl",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
        "Accept": "application/json",
        "Accept-Language": "pl,en;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Referer": "https://platformaofertowa.pl/",
        "content-type": "application/json",
        "Origin": "https://platformaofertowa.pl",
        "Sec-GPC": "1",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-site",
        "Priority": "u=4",
        "TE": "trailers",
    }

    attachment_links = set()
    base_api_sleep = 300
    api_penalty_multiplier = 1

    while True:
        async with semaphore:
            await asyncio.sleep(random.uniform(2.0, 4.0))

            async with session.get(api_attachments_url, headers=attachments_list_headers) as resp:
                if resp.status == 429:
                    current_sleep = base_api_sleep * api_penalty_multiplier
                    logger.warning(f"Otrzymano HTTP 429 dla API załączników ({notice_id}). Czyszczę sesję i usypiam na {current_sleep / 60} min...")
                    session.cookie_jar.clear()
                    await asyncio.sleep(current_sleep)
                    api_penalty_multiplier += 1
                    continue

                resp.raise_for_status()

                attachments_data = await resp.json()
                for att in attachments_data:
                    att_id = att.get("id")
                    if att_id:
                        download_url = f"https://api.platformaofertowa.pl/tenders/attachment/download/{att_id}"
                        attachment_links.add(download_url)
                return attachment_links


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
    valid_extensions = {".zip", ".7z", ".rar", ".doc", ".docx", ".xls", ".xlsx", ".pdf"}
    has_valid_extension = Path(filename).suffix.lower() in valid_extensions

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

    base_sleep_on_limit = 300
    penalty_multiplier = 1

    while True:
        async with semaphore:
            await asyncio.sleep(random.uniform(2.0, 4.0))

            har_headers = {
                "Host": "api.platformaofertowa.pl",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pl,en;q=0.9,en-US;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Sec-GPC": "1",
                "Alt-Used": "api.platformaofertowa.pl",
                "Connection": "keep-alive",
                "Referer": "https://platformaofertowa.pl/",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "same-site",
                "Sec-Fetch-User": "?1",
                "Priority": "u=0, i",
                "TE": "trailers",
            }

            async with session.get(url, headers=har_headers) as response:
                if response.status == 429:
                    current_sleep = base_sleep_on_limit * penalty_multiplier
                    logger.warning(f"Otrzymano HTTP 429 dla {url}. Czyszczę sesję i usypiam scraper na {current_sleep / 60} min...")
                    session.cookie_jar.clear()
                    await asyncio.sleep(current_sleep)
                    penalty_multiplier += 1
                    continue

                response.raise_for_status()

                if response.content_length in (154853, 528025):
                    current_sleep = base_sleep_on_limit * penalty_multiplier
                    logger.warning(f"Wykryto PDF z ostrzeżeniem o limitach! Czyszczę sesję i usypiam scraper na {current_sleep / 60} min...")
                    session.cookie_jar.clear()
                    await asyncio.sleep(current_sleep)
                    penalty_multiplier += 1
                    continue

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
                        sanitized_cd_filename = Path(cd_filename).name
                        if sanitized_cd_filename:
                            filename = sanitized_cd_filename
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


def _extract_zip_sync(filepath: Path, extract_to: Path) -> str:
    try:
        if not zipfile.is_zipfile(filepath):
            logger.error(f"Plik {filepath.name} nie jest poprawnym archiwum ZIP (uszkodzony pobrany plik).")
            return "corrupted_archive"

        extract_to.mkdir(parents=True, exist_ok=True)
        base_dir = extract_to.resolve()
        with zipfile.ZipFile(filepath, "r") as zip_ref:
            for member in zip_ref.infolist():
                member_path = Path(member.filename.replace("\\", "/"))
                if member_path.is_absolute():
                    raise ValueError(f"Niebezpieczna ścieżka w archiwum ZIP: {member.filename}")

                destination = (base_dir / member_path).resolve()
                if destination != base_dir and base_dir not in destination.parents:
                    raise ValueError(f"Niebezpieczna ścieżka w archiwum ZIP: {member.filename}")

                if member.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue

                destination.parent.mkdir(parents=True, exist_ok=True)
                with zip_ref.open(member, "r") as source, destination.open("wb") as target:
                    target.write(source.read())

        logger.info(f"Pomyślnie rozpakowano archiwum: {filepath.name} -> {extract_to.name}/")
        return "success"
    except zipfile.BadZipFile:
        logger.error(f"Błąd struktury ZIP w pliku {filepath.name}.")
        return "bad_zip_file"
    except Exception as e:
        logger.error(f"Nieoczekiwany błąd podczas rozpakowywania {filepath.name}: {e}")
        return f"error: {str(e)}"
    finally:
        filepath.unlink(missing_ok=True)


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
        "sort_by": "publication_date_desc",
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
) -> dict | None:
    notice_id = str(item.get("id"))
    logger.info(f"Rozpoczęto analizę ogłoszenia: {notice_id}")
    original_url = extract_best_url(item)

    try:
        raw_html = await fetch_html(session, original_url, html_semaphore)
    except Exception as e:
        logger.error("==== BŁĄD POBIERANIA ====")
        logger.error(f"ID ogłoszenia: {notice_id}")
        logger.error(f"Nie udało się pobrać strony ogłoszenia {original_url}: {e!r}")
        logger.error("=========================")
        return None

    await save_to_file(RAW_DIR / f"{notice_id}.html", raw_html)

    doc = BeautifulSoup(raw_html, "lxml")
    clean_description = doc.get_text(separator="\n", strip=True)

    try:
        attachment_links = await fetch_attachments_metadata(session, notice_id, html_semaphore)
    except Exception as e:
        logger.error(f"Ostateczny błąd pobierania API po kilku próbach dla {notice_id}: {e}")
        attachment_links = set()

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

    formatted_attachments = []
    extraction_tasks = []
    extraction_indices = []

    for att in valid_attachments_metadata:
        filename = att.get("filename", "")
        local_path_str = att.get("local_path")
        filepath = BASE_DIR / local_path_str
        is_zip = filename.lower().endswith(".zip")

        att_entry = {
            "url": att.get("url"),
            "filename": filename,
            "local_path": local_path_str,
            "downloaded": att.get("downloaded", False),
            "is_zip": is_zip,
            "extracted_status": "not_applicable" if not is_zip else "pending",
        }

        if is_zip:
            extract_dir = filepath.parent / filepath.stem

            task = asyncio.to_thread(_extract_zip_sync, filepath, extract_dir)
            extraction_tasks.append(task)
            extraction_indices.append(len(formatted_attachments))

        formatted_attachments.append(att_entry)

    if extraction_tasks:
        extraction_results = await asyncio.gather(*extraction_tasks, return_exceptions=True)
        for idx, res in zip(extraction_indices, extraction_results, strict=True):
            if isinstance(res, Exception):
                formatted_attachments[idx]["extracted_status"] = f"exception: {str(res)}"
            else:
                formatted_attachments[idx]["extracted_status"] = res

    raw_cpvs = item.get("cpvCodes") or item.get("cpvs") or []
    cpv_codes = [str(c.get("code", c)) if isinstance(c, dict) else str(c) for c in raw_cpvs]

    raw_buyers = item.get("buyers") or item.get("contractingAuthorities") or item.get("issuers") or []
    if not raw_buyers and item.get("companyName"):
        raw_buyers = [item]

    issuers = []
    for buyer in raw_buyers:
        if not buyer:
            continue
        address_info = buyer.get("address") or {}

        issuers.append(
            {
                "title": buyer.get("name") or buyer.get("companyName") or item.get("companyName") or buyer.get("title"),
                "address": {
                    "street": address_info.get("street") or buyer.get("street"),
                    "city": address_info.get("city") or buyer.get("city") or item.get("locality"),
                    "postalCode": address_info.get("postalCode") or buyer.get("postalCode") or buyer.get("zipCode"),
                    "country": address_info.get("country") or buyer.get("country"),
                },
            }
        )

    parsed_data = {
        "id": notice_id,
        "enrichment": {
            "tags": item.get("tags") or item.get("enrichment", {}).get("tags") or [],
            "industry": item.get("industry") or item.get("enrichment", {}).get("industry"),
            "nuts3": item.get("nuts3") or item.get("enrichment", {}).get("nuts3") or [],
        },
        "createdAt": item.get("createdAt"),
        "publicationDate": item.get("publicationDate"),
        "submittingOffersDeadline": item.get("submittingOffersDeadline") or item.get("submitting_offers_deadline"),
        "cpvCodes": [c for c in cpv_codes if c],
        "issuers": issuers,
        "title": item.get("title"),
        "description": clean_description,
        "referenceNumber": item.get("referenceNumber") or item.get("signature"),
        "contractNature": item.get("orderType") or item.get("contractNature"),
        "scraper_url": original_url,
        "scraper_attachments": formatted_attachments,
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
        "sort_by": "publication_date_desc",
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

    html_semaphore = asyncio.Semaphore(1)
    file_semaphore = asyncio.Semaphore(1)

    total_downloaded = 0

    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            total_pages = await get_total_pages(session)

            logger.info(f"Rozpoczęcie pobierania {total_pages} stron...")

            for page in range(1, total_pages + 1):
                try:
                    res = await process_list_page(session, page, last_run_date, html_semaphore, file_semaphore)
                    total_downloaded += res
                except Exception as e:
                    logger.error(f"Błąd przy przetwarzaniu strony {page}: {e!r}")

    finally:
        set_last_run_date(current_run_date)
        logger.info(f"Zakończono pracę programu. Łącznie pobrano {total_downloaded} nowych przetargów.")


if __name__ == "__main__":
    asyncio.run(main())
