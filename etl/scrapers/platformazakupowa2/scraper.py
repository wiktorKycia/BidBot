import asyncio
import logging
import re
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiohttp
import backoff
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

from etl.settings import (
    ATTACHMENTS_DIR,
    MAX_ATTACHMENT_SIZE,
    MAX_ATTACHMENTS,
    MAX_UNCOMPRESSED_SIZE,
    MAX_ZIP_FILES,
    MAX_ZIP_RATIO,
    PARSED_DIR,
    RAW_DIR,
    setup_logging,
)
from etl.utils import save_json, save_to_file

setup_logging()
logger = logging.getLogger(__name__)

BASE_URL = "https://platformazakupowa.pl"
ALL_RESOURCES_URL = "https://platformazakupowa.pl/all"

ORDER_TYPE_DICT = {
    "Dostawa": "Dostawy",
    "Usługa": "Usługi",
    "Robota budowlana": "Roboty budowlane",
}


class DownloadIntegrityError(Exception):
    """Błąd rzucany, gdy rozmiar pliku nie zgadza się z nagłówkiem Content-Length."""

    pass


def check_if_notice_html_already_downloaded(notice_id: str) -> bool:
    return (RAW_DIR / f"{notice_id}.html").exists()


def is_tender_open(deadline_dt: datetime) -> bool:
    if deadline_dt:
        return deadline_dt > datetime.now(UTC)
    return False


def sanitize_filename(filename: str, fallback: str = "attachment") -> str:
    invalid_filename_chars_pattern = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]')

    if not filename:
        return fallback

    clean_path = filename.replace("\\", "/")
    basename = Path(clean_path).name.strip()
    sanitized = invalid_filename_chars_pattern.sub("_", basename).strip(" .")

    if not sanitized:
        return fallback
    return sanitized


@backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ClientResponseError, DownloadIntegrityError), max_tries=3)
async def download_file(session: aiohttp.ClientSession, url: str, target_dir: Path, filename: str, semaphore: asyncio.Semaphore):
    file_path = target_dir / filename
    if file_path.exists() and file_path.stat().st_size > 0:
        return True

    async with semaphore:
        try:
            async with session.get(url, timeout=60) as response:
                response.raise_for_status()
                content_length = int(response.headers.get("Content-Length", 0))
                if content_length > MAX_ATTACHMENT_SIZE:
                    logger.warning(f"Plik odrzucony (nagłówek): {filename} przekracza {MAX_ATTACHMENT_SIZE} bajtów.")
                    return False

                downloaded_size = 0
                with open(file_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        downloaded_size += len(chunk)

                        if downloaded_size > MAX_ATTACHMENT_SIZE:
                            file_path.unlink(missing_ok=True)
                            logger.warning(f"Plik odrzucony (stream): {filename} przekroczył limit wielkości w trakcie pobierania.")
                            return False

                        f.write(chunk)

            if file_path.suffix.lower() == ".zip":
                success = await asyncio.to_thread(safe_extract_zip, file_path, target_dir)

                if success:
                    file_path.unlink(missing_ok=True)
                    return "extracted"
                else:
                    file_path.unlink(missing_ok=True)
                    logger.error(f"Nieudane/niebezpieczne rozpakowywanie pliku: {filename}")
                    return False

            return True

        except Exception as e:
            if file_path.exists():
                file_path.unlink(missing_ok=True)
            logger.error(f"Błąd podczas pobierania {filename}: {e}")
            return False


@backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ClientResponseError), max_tries=3)
async def fetch_page(session: aiohttp.ClientSession, page_number: int, semaphore: asyncio.Semaphore) -> str:
    params = {"page": page_number, "limit": 100}
    async with semaphore:
        try:
            async with session.get(ALL_RESOURCES_URL, params=params, timeout=60) as response:
                response.raise_for_status()
                return await response.text()
        except (TimeoutError, aiohttp.ClientError, aiohttp.ClientResponseError) as e:
            logger.error(f"Błąd fetch_page {page_number} (próba zostanie powtórzona): {e}")
            raise
        except Exception as e:
            logger.error(f"Błąd fetch_page {page_number}: {e}")
            raise


@backoff.on_exception(backoff.expo, (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ClientResponseError), max_tries=3)
async def fetch_notice_details(session: aiohttp.ClientSession, notice_url: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            async with session.get(notice_url, headers={"Accept-Language": "pl"}) as response:
                response.raise_for_status()
                return await response.text()
        except (TimeoutError, aiohttp.ClientError, aiohttp.ClientResponseError) as e:
            logger.error(f"Błąd fetch_notice_details {notice_url} (próba zostanie powtórzona): {e}")
            raise
        except Exception as e:
            logger.error(f"Błąd fetch_notice_details {notice_url}: {e}")
            raise


async def process_notice_details(session: aiohttp.ClientSession, notice_url: str, notice_id: str, semaphore: asyncio.Semaphore) -> dict:
    data = await fetch_notice_details(session, notice_url, semaphore)
    if not data:
        return {}

    notice_doc = BeautifulSoup(data, "lxml")
    organisation = "Nie podano nazwy"
    publication_date_dt = None
    order_type = None

    li_items = notice_doc.find_all("li", class_="proceeding-info-list-item")
    for li in li_items:
        div = li.find("div")
        if not div:
            continue

        label_text = div.text.strip()
        if "Organizacja" in label_text:
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                organisation = " ".join(texts[1:])

        elif "Rodzaj" in label_text:
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                order_type = " ".join(texts[1:]).strip()
                if order_type in ORDER_TYPE_DICT:
                    order_type = ORDER_TYPE_DICT[order_type]  # zamiana na liczbę mnogą
                else:
                    logger.warning(f"Nieznany rodzaj zamówienia: {order_type}, dostępne rodzaje: {' '.join(ORDER_TYPE_DICT.keys())}")

        elif any(x in label_text for x in ["Opublikowano", "Zamieszczenia"]):
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                try:
                    raw_date = " ".join(texts[1:]).strip()
                    clean_date_str = " ".join(raw_date.split()[:2])
                    parsed_dt = datetime.strptime(clean_date_str, "%Y-%m-%d %H:%M:%S")
                    publication_date_dt = parsed_dt.replace(tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(UTC)
                except ValueError:
                    logger.warning("Błąd parsowania daty publikacji: %r", raw_date)

    attachments_list = []
    table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    if table and table.tbody:
        for row in table.tbody.find_all("tr"):
            a_tag = row.find("a", class_="proceeding-file-download")
            td_filename = row.find("td", class_="text-left")

            if a_tag and td_filename:
                file_path = Path(sanitize_filename(td_filename.text.strip()))
                filename = file_path.name

                attachments_list.append(
                    {
                        "url": urljoin(BASE_URL, a_tag["href"]),
                        "filename": filename,
                        "local_path": str(Path(notice_id) / filename) if filename else None,
                        "downloaded": False,
                        "is_zip": file_path.suffix.lower() == ".zip",
                    }
                )

    if attachments_list:
        notice_dir = ATTACHMENTS_DIR / notice_id

        downloadable = [a for a in attachments_list if a["filename"]]
        to_download = downloadable[:MAX_ATTACHMENTS]

        if to_download:
            if not notice_dir.exists():
                notice_dir.mkdir(parents=True, exist_ok=True)

            tasks = []
            for att in to_download:
                t = download_file(session, att["url"], notice_dir, att["filename"], semaphore)
                tasks.append(t)

            results = await asyncio.gather(*tasks, return_exceptions=True)

            for i, res in enumerate(results):
                if res is True:
                    to_download[i]["downloaded"] = True
                elif res == "extracted":
                    to_download[i]["downloaded"] = True
                    to_download[i]["extracted_status"] = "success"
                elif isinstance(res, Exception):
                    logger.error(f"Błąd gather załącznika {notice_id}: {res}")

    requirements = notice_doc.find("div", {"id": "requirements"})
    desc = requirements.get_text(strip=True) if requirements else "brak"

    return {
        "client_name": organisation,
        "publication_date_dt": publication_date_dt,
        "description": desc,
        "order_type": order_type,
        "raw_html": notice_doc.prettify(),
        "attachments": attachments_list,
    }


def safe_extract_zip(zip_path: Path, extract_dir: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            infolist = zf.infolist()

            if len(infolist) > MAX_ZIP_FILES:
                return False

            total_size = 0
            for info in infolist:
                total_size += info.file_size
                if total_size > MAX_UNCOMPRESSED_SIZE:
                    return False

            compressed_size = zip_path.stat().st_size or 1
            if (total_size / compressed_size) > MAX_ZIP_RATIO:
                return False

            zf.extractall(path=extract_dir)
            return True

    except zipfile.BadZipFile:
        return False


async def process_page(session: aiohttp.ClientSession, page_number: int, semaphore: asyncio.Semaphore) -> int:
    logger.info(f"Strona {page_number}...")
    data = await fetch_page(session, page_number, semaphore)
    if not data:
        return 0

    doc = BeautifulSoup(data, "lxml")
    notices = doc.find_all("div", class_="product-info")

    async def process_single_notice(notice_div) -> bool:
        a_tag = notice_div.find("a")
        if not a_tag:
            return False

        notice_url = urljoin(BASE_URL, a_tag["href"])
        notice_id = notice_url.split("/")[-1]

        if check_if_notice_html_already_downloaded(notice_id):
            return False

        span = notice_div.find("span", class_="auction-time")
        if not span or not span.find("b") or "title" not in span.b.attrs:
            return False

        try:
            title_text = span.b["title"]
            deadline_str = " ".join(title_text.split()[:2]).strip()
            deadline_dt = datetime.strptime(deadline_str, "%d-%m-%Y %H:%M:%S").replace(tzinfo=UTC)
        except Exception:
            return False

        if not is_tender_open(deadline_dt):
            return False

        details = await process_notice_details(session, notice_url, notice_id, semaphore)
        if not details:
            return False

        pub_date = None
        if details.get("publication_date_dt"):
            pub_date = details["publication_date_dt"].isoformat()

        parsed_data = {
            "id": notice_id,
            "url": notice_url,
            "title": a_tag.text.strip(),
            "publication_date": pub_date,
            "submitting_offers_date": deadline_dt.isoformat(),
            "client_name": details.get("client_name"),
            "description": details.get("description"),
            "attachments": details.get("attachments"),
        }

        await save_to_file(RAW_DIR / f"{notice_id}.html", details["raw_html"])
        await save_json(PARSED_DIR / f"{notice_id}.json", parsed_data)
        return True

    tasks = [process_single_notice(n) for n in notices]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Wyjątek na stronie {page_number}: {r}")

    return sum(1 for r in results if r is True)


async def get_pages_number(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    try:
        data = await fetch_page(session, 1, semaphore)
        doc = BeautifulSoup(data, "lxml")

        if not data:
            return 0  # nie wiem co tu dać, czy jak html jest pusty to zwrócić, że jest 0 stron, czy jakiś error

        return int(doc.select("ul.pagination li")[-2].get_text(strip=True))
    except IndexError as e:
        logger.error(f"Pagination interface is not being displayed correctly, cannot get the number of the last page: {e}")
        raise
    except ValueError as e:
        logger.error(f"Invalid pagination value, cannot parse last page number: {e}")
        raise
    except AttributeError as e:
        logger.error(f"Pagination structure is missing expected elements, cannot get last page number: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        raise


async def main():
    semaphore = asyncio.Semaphore(3)

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)

    async with aiohttp.ClientSession(connector=connector) as session:
        total_pages = await get_pages_number(session, semaphore)
        logger.info(f"Znaleziono stron: {total_pages}")

        total_new = 0
        for p in range(1, total_pages + 1):
            count = await process_page(session, p, semaphore)
            total_new += count
            if count == 0 and p > 1:
                break

    logger.info(f"Koniec. Pobrano nowych: {total_new}")


if __name__ == "__main__":
    asyncio.run(main())
