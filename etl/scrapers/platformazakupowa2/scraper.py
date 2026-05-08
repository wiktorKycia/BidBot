import asyncio
import contextlib
import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import aiofiles
import aiohttp
import backoff
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

from etl.settings import ATTACHMENTS_DIR, MAX_ATTACHMENTS, PARSED_DIR, RAW_DIR, setup_logging

setup_logging()
logger = logging.getLogger(__name__)

BASE_URL = "https://platformazakupowa.pl"
ALL_RESOURCES_URL = "https://platformazakupowa.pl/all"


class DownloadIntegrityException(Exception):
    """Błąd rzucany, gdy rozmiar pliku nie zgadza się z nagłówkiem Content-Length."""
    pass


def is_downloaded(notice_id: str) -> bool:
    return (RAW_DIR / f"{notice_id}.html").exists()


def is_tender_open(deadline_dt: datetime) -> bool:
    if deadline_dt:
        return deadline_dt > datetime.now(UTC)
    return False


async def save_json(filepath: Path, data: dict):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        json_data = json.dumps(data, ensure_ascii=False, indent=2)
        await f.write(json_data)


async def save_html(filepath: Path, data: str):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(data)


def sanitize_filename(filename: str, fallback: str = "attachment") -> str:
    if not filename:
        return fallback

    clean_path = filename.replace("\\", "/")
    basename = Path(clean_path).name.strip()
    sanitized = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f]', "_", basename).strip(" .")

    if not sanitized:
        return fallback
    return sanitized


@backoff.on_exception(
    backoff.expo,
    (aiohttp.ClientError, asyncio.TimeoutError, aiohttp.ClientResponseError, DownloadIntegrityException),
    max_tries=3
)
async def download_file(session: aiohttp.ClientSession, url: str, output_dir: Path, filename: str, semaphore: asyncio.Semaphore) -> bool:
    filepath = output_dir / filename
    if filepath.exists() and filepath.stat().st_size > 0:
        return True

    m = hashlib.md5(url.encode())
    url_hash = m.hexdigest()[:8]
    temp_filepath = filepath.with_suffix(f".{url_hash}.tmp")

    async with semaphore:
        try:
            async with session.get(url, timeout=60) as response:
                response.raise_for_status()

                size_counter = 0
                async with aiofiles.open(temp_filepath, "wb") as f:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if chunk:
                            await f.write(chunk)
                            size_counter += len(chunk)

                cl_raw = response.headers.get("Content-Length")
                if cl_raw and cl_raw.isdigit():
                    if int(cl_raw) != size_counter:
                        raise DownloadIntegrityException(f"Niezgodny rozmiar: {size_counter}/{cl_raw}")

                temp_filepath.replace(filepath)
                return True

        except (aiohttp.ClientError, asyncio.TimeoutError, DownloadIntegrityException) as e:
            logger.error(f"Błąd pobierania {filename} (próba zostanie powtórzona): {e}")

            if temp_filepath.exists():
                with contextlib.suppress(Exception):
                    temp_filepath.unlink()

            raise e

        except Exception as e:
            logger.critical(f"Błąd pobierania {filename}: {e}")
            if temp_filepath.exists():
                with contextlib.suppress(Exception):
                    temp_filepath.unlink()
            return False

async def fetch_page(session: aiohttp.ClientSession, page_number: int, semaphore: asyncio.Semaphore) -> str:
    params = {"page": page_number, "limit": 100}
    async with semaphore:
        try:
            async with session.get(ALL_RESOURCES_URL, params=params) as response:
                if response.status != 200:
                    logger.error(f"Błąd HTTP {response.status} dla str. {page_number}")
                    return ""
                return await response.text()
        except Exception as e:
            logger.error(f"Błąd fetch_page {page_number}: {e}")
            return ""


async def fetch_notice_details(session: aiohttp.ClientSession, notice_url: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        try:
            async with session.get(notice_url, headers={"Accept-Language": "pl"}) as response:
                if response.status == 200:
                    return await response.text()
                return ""
        except Exception:
            return ""


async def process_notice_details(session: aiohttp.ClientSession, notice_url: str, notice_id: str, semaphore: asyncio.Semaphore) -> dict:
    data = await fetch_notice_details(session, notice_url, semaphore)
    if not data:
        return {}

    notice_doc = BeautifulSoup(data, "lxml")
    organisation = "Nie podano nazwy"
    publication_date_dt = None

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

        elif any(x in label_text for x in ["Opublikowano", "Zamieszczenia"]):
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                try:
                    raw_date = " ".join(texts[1:]).strip()
                    clean_date_str = " ".join(raw_date.split()[:2])
                    parsed_dt = datetime.strptime(clean_date_str, "%Y-%m-%d %H:%M:%S")
                    publication_date_dt = parsed_dt.replace(tzinfo=ZoneInfo("Europe/Warsaw")).astimezone(UTC)
                except Exception:
                    pass

    attachments_list = []
    table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    if table and table.tbody:
        for row in table.tbody.find_all("tr"):
            a_tag = row.find("a", class_="proceeding-file-download")
            td_filename = row.find("td", class_="text-left")

            if a_tag and td_filename:
                filename = sanitize_filename(td_filename.text.strip())
                ext = Path(filename).suffix.lower().lstrip(".")

                if ext in ["zip", "7z"]:
                    filename = None

                attachments_list.append(
                    {
                        "url": urljoin(BASE_URL, a_tag["href"]),
                        "filename": filename,
                        "local_path": str(Path(notice_id) / filename) if filename else None,
                        "downloaded": False,
                    }
                )

    if attachments_list:
        notice_dir = ATTACHMENTS_DIR / notice_id

        downloadable = []
        for a in attachments_list:
            if a["filename"]:
                downloadable.append(a)

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
                elif isinstance(res, Exception):
                    logger.error(f"Błąd gather załącznika {notice_id}: {res}")

    requirements = notice_doc.find("div", {"id": "requirements"})
    desc = requirements.get_text(strip=True) if requirements else "brak"

    return {
        "client_name": organisation,
        "publication_date_dt": publication_date_dt,
        "description": desc,
        "raw_html": notice_doc.prettify(),
        "attachments": attachments_list,
    }


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

        if is_downloaded(notice_id):
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

        await save_html(RAW_DIR / f"{notice_id}.html", details["raw_html"])
        await save_json(PARSED_DIR / f"{notice_id}.json", parsed_data)
        return True

    tasks = [process_single_notice(n) for n in notices]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            logger.error(f"Wyjątek na stronie {page_number}: {r}")

    return sum(1 for r in results if r is True)


async def get_pages_number(session: aiohttp.ClientSession, semaphore: asyncio.Semaphore):
    data = await fetch_page(session, 1, semaphore)
    if not data:
        return 1

    doc = BeautifulSoup(data, "lxml")
    ul = doc.find("ul", class_="pagination")
    if not ul:
        return 1

    li_list = ul.find_all("li")
    try:
        last_page_text = li_list[-2].a.text
        return int(last_page_text)
    except Exception:
        return 1


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
