import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import aiofiles
import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

API_URL = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"

NOTICE_TYPES = ["ContractNotice", "SmallContractNotice"]

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"
PARSED_DIR = DATA_DIR / "parsed"

ORDER_TYPE_DICT = {"Delivery": "Dostawy", "Services": "Usługa", "Works": "Robota budowlana"}


def setup_directories():
    """Tworzy strukturę katalogów."""
    for directory in [RAW_DIR, PARSED_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def is_downloaded(object_id: str) -> bool:
    """Deduplikacja: Sprawdza czy dany plik już istnieje na dysku."""
    return (RAW_DIR / f"{object_id}.json").exists()


def is_tender_open(raw_data: dict) -> bool:
    """Sprawdza, czy termin składania ofert jest w przyszłości.
    Zwraca True, jeśli przetarg jest otwarty.
    """
    deadline_str = raw_data.get("submittingOffersDate")

    if not deadline_str:
        return False

    try:
        deadline = datetime.fromisoformat(deadline_str)
        now = datetime.now(UTC)

        return deadline > now
    except ValueError as e:
        print(f"Błąd parsowania daty {deadline_str}: {e}")
        return False


def parse_notice(raw_data: dict) -> dict:
    """Normalizacja danych do ujednoliconego formatu dla bota.
    Wyczyszczenie HTML z treści ogłoszenia oraz ekstrakcja linków do dokumentacji.
    """
    html_body = raw_data.get("htmlBody", "")
    description_text = ""
    attachments = []

    if html_body:
        soup = BeautifulSoup(html_body, "html.parser")

        description_text = soup.get_text(separator=" ", strip=True)

        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            text = a_tag.get_text(strip=True)

            if href.startswith("http") and not any(att["url"] == href for att in attachments):
                attachments.append({"text": text or "Link", "url": href})

    object_id = raw_data.get("objectId")

    return {
        "id": object_id,
        "source": "ezamowienia_bzp",
        "url": f"https://ezamowienia.gov.pl/mo-client-board/bzp/notice-details/id/{object_id}",
        "title": raw_data.get("orderObject"),
        "order_type": ORDER_TYPE_DICT.get(raw_data.get("orderType")),
        "publication_date": raw_data.get("publicationDate"),
        "submitting_offers_date": raw_data.get("submittingOffersDate"),
        "notice_number": raw_data.get("bzpNumber") or raw_data.get("noticeNumber"),
        "client_name": raw_data.get("organizationName"),
        "description": description_text,
        "attachments": attachments,
    }


async def save_json(filepath: Path, data: dict):
    """Asynchroniczny zapis pliku JSON."""
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))


async def fetch_page(
    session: aiohttp.ClientSession,
    notice_type: str,
    search_after: str = None,
) -> list:
    """Pobiera pojedynczą stronę wyników dla danego typu ogłoszenia."""
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
            print(f"Błąd HTTP {response.status} dla {notice_type}")
            return []

        try:
            return await response.json()
        except Exception as e:
            print(f"Błąd parsowania JSON dla {notice_type}: {e}")
            return []


async def process_notice_type(session: aiohttp.ClientSession, notice_type: str):
    """Przechodzi przez wszystkie strony danego typu ogłoszenia i przetwarza je."""
    search_after = None
    page = 1
    total_downloaded = 0

    print(f"Rozpoczęto pobieranie typu: {notice_type}")

    while True:
        data = await fetch_page(session, notice_type, search_after)

        if not data:
            break

        print(
            f"[{notice_type}] Przeszukiwanie strony {page} (Pobranych z API: {len(data)})",
        )

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

        # ostatnia strona, kiedy mniej niż 500
        if len(data) < 500:
            break

    print(
        f"Zakończono typ {notice_type}. Pobranych i zapisanych otwartych przetargów: {total_downloaded}",
    )


async def scrape():
    setup_directories()

    print("Inicjalizacja scrapera API e-Zamówienia (BZP)...")

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [process_notice_type(session, nt) for nt in NOTICE_TYPES]
        await asyncio.gather(*tasks)

    print("Zakończono.")


if __name__ == "__main__":
    asyncio.run(scrape())
