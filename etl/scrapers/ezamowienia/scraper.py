import asyncio
import logging
from datetime import UTC, datetime, timedelta

import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

from etl.settings import ATTACHMENTS_DIR, PARSED_DIR, RAW_DIR, setup_logging
from etl.utils import save_json

setup_logging()
logger = logging.getLogger(__name__)

API_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"

NOTICE_TYPES = ["ContractNotice", "SmallContractNotice"]

ORDER_TYPE_DICT = {
    "Delivery": "Dostawy",
    "Services": "Usługi",
    "Works": "Roboty budowlane",
}


def setup_directories():
    for directory in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def is_downloaded(object_id: str) -> bool:
    return (RAW_DIR / f"{object_id}.json").exists()


def is_tender_open(raw_data: dict) -> bool:
    deadline_str = raw_data.get("submittingOffersDate")

    if not deadline_str:
        return False

    try:
        deadline = datetime.fromisoformat(deadline_str)
        now = datetime.now(UTC)

        return deadline > now
    except ValueError as e:
        logger.exception(f"Błąd parsowania daty {deadline_str}: {e}")
        return False


def parse_notice(raw_data: dict) -> dict:
    html_body = raw_data.get("htmlBody", "")
    description_text = ""
    attachments = []

    if html_body:
        soup = BeautifulSoup(html_body, "lxml")

        description_text = soup.get_text(separator="\n", strip=True)

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if href.startswith("http") and not any(att["url"] == href for att in attachments):
                attachments.append({"url": href, "filename": None, "local_path": None, "downloaded": False, "is_zip": href.lower().endswith(".zip"), "extracted_status": None})

    object_id = str(raw_data.get("objectId"))

    issuers = []
    org_name = raw_data.get("organizationName")
    if org_name:
        issuers.append({"title": org_name, "address": {"street": None, "city": raw_data.get("organizationCity"), "postalCode": None, "country": None}})

    return {
        "id": object_id,
        "enrichment": {"tags": [], "industry": None, "nuts3": []},
        "createdAt": None,
        "publicationDate": raw_data.get("publicationDate"),
        "submittingOffersDeadline": raw_data.get("submittingOffersDate"),
        "cpvCodes": [],
        "issuers": issuers,
        "title": raw_data.get("orderObject"),
        "description": description_text,
        "referenceNumber": raw_data.get("bzpNumber") or raw_data.get("noticeNumber"),
        "contractNature": ORDER_TYPE_DICT.get(raw_data.get("orderType")),
        "scraper_url": f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{object_id}",
        "scraper_attachments": attachments,
    }


async def fetch_page(
    session: aiohttp.ClientSession,
    notice_type: str,
    search_after: str = None,
) -> list:
    start_date = (datetime.now() - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")
    end_date = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

    params = {
        "NoticeType": notice_type,
        "PublicationDateFrom": start_date,
        "PublicationDateTo": end_date,
        "PageSize": 500,
    }

    if search_after:
        params["SearchAfter"] = search_after

    async with session.get(API_URL, params=params) as response:
        if response.status != 200:
            logger.error(f"Błąd HTTP {response.status} dla {notice_type}")
            return []

        try:
            return await response.json()
        except Exception as e:
            logger.error(f"Błąd parsowania JSON dla {notice_type}: {e}")
            return []


async def process_notice_type(session: aiohttp.ClientSession, notice_type: str):
    search_after = None
    page = 1
    total_downloaded = 0

    logger.info(f"Rozpoczęto pobieranie typu: {notice_type}")

    while True:
        data = await fetch_page(session, notice_type, search_after)

        if not data:
            break

        logger.info(f"[{notice_type}] Przeszukiwanie strony {page} (Pobranych z API: {len(data)})")

        for item in data:
            object_id = item.get("objectId")

            if not is_tender_open(item):
                continue

            if not object_id or is_downloaded(object_id):
                continue

            raw_filepath = RAW_DIR / f"{object_id}.json"
            await save_json(raw_filepath, item)

            parsed_data = parse_notice(item)
            parsed_filepath = PARSED_DIR / f"{object_id}.json"
            await save_json(parsed_filepath, parsed_data)

            total_downloaded += 1

        search_after = data[-1].get("objectId")
        page += 1

        if len(data) < 500:
            break

    logger.info(f"Zakończono typ {notice_type}. Pobranych i zapisanych otwartych przetargów: {total_downloaded}")


async def scrape():
    setup_directories()

    logger.info("Inicjalizacja scrapera API e-Zamówienia (BZP)...")

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_notice_type(session, nt) for nt in NOTICE_TYPES]
        await asyncio.gather(*tasks)

    logger.info("Zakończono.")


if __name__ == "__main__":
    asyncio.run(scrape())
