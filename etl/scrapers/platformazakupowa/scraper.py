import asyncio
import json
from pathlib import Path

import aiofiles
import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup
# import requests
from datetime import datetime, timezone

BASE_URL = "https://platformazakupowa.pl"
ALL_RESOURCES_URL = "https://platformazakupowa.pl/all"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw_html"
PARSED_DIR = DATA_DIR / "parsed"

def setup_directories():
    """Tworzy strukturę katalogów."""
    for directory in [RAW_DIR, PARSED_DIR]:
        directory.mkdir(parents=True, exist_ok=True)


def is_downloaded(notice_id: str) -> bool:
    """De-duplikacja: Sprawdza, czy dany plik już istnieje na dysku."""
    return (RAW_DIR / f"{notice_id}.json").exists()

def is_tender_open(deadline_str: str) -> bool:
    """
    Sprawdza, czy termin składania ofert jest w przyszłości.
    Zwraca True, jeśli przetarg jest otwarty.
    """
    if not deadline_str:
        return False

    try:
        deadline = datetime.fromisoformat(deadline_str.replace('Z', '+00:00'))
        now = datetime.now(timezone.utc)

        return deadline > now
    except ValueError as e:
        print(f"Błąd parsowania daty {deadline_str}: {e}")
        return False

async def save_json(filepath: Path, data: dict):
    """Asynchroniczny zapis pliku JSON."""
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))

async def save_html(filepath: Path, data: str):
    """Asynchroniczny zapis pliku JSON."""
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(data)

async def fetch_page(session: aiohttp.ClientSession, page_number: int) -> str:
    """Pobiera pojedynczą stronę wyników."""
    params = {
        "page": page_number,
        "limit": 100
    }
    async with session.get(ALL_RESOURCES_URL, params=params) as response:
        if response.status != 200:
            print(f"Błąd HTTP {response.status} dla strony {page_number}")
            return ""

        try:
            return await response.text()
        except Exception as e:
            print(f"Błąd parsowania tekstu strony {page_number}: {e}")
            return ""

async def process_page(session: aiohttp.ClientSession, page_number: int):
    print(f"Rozpoczęto pobieranie strony {page_number}")
    total_notices_downloaded: int = 0
    data = await fetch_page(session, page_number)

    if not data:
        return

    doc = BeautifulSoup(data, "html.parser")

    notices = doc.find_all("div", "product-info")

    for notice in notices:
        notice_id = notice.a['href'].split('/')[-1] # ten numerek po słowie: /transakcja/
        notice_name = notice.a.text.strip()
        notice_url = f"{BASE_URL}{notice.a['href']}"
        span = notice.find("span", "auction-time")
        submitting_offers_date_str = " ".join(span.b['title'].split()[:2]).strip()
        submitting_offers_date = datetime.strptime(submitting_offers_date_str, '%d-%m-%Y %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%SZ')

        details = await process_notice_details(session, notice_url)

        parsed_data = {
            "id": notice_id,
            "source": "platformazakupowa.pl",
            "url": notice_url,
            "title": notice_name,
            "publication_date": "Zara bedzie",
            "submitting_offers_date": submitting_offers_date,
            "notice_number": "Unknown",
            **details
        }

        if not is_tender_open(submitting_offers_date):
            continue

        if not notice_id or is_downloaded(notice_id):
            continue

        raw_filepath = RAW_DIR / f"{notice_id}.html"
        await save_html(raw_filepath, data)

        parsed_filepath = PARSED_DIR / f"{notice_id}.json"
        await save_json(parsed_filepath, parsed_data)

        total_notices_downloaded += 1

    print(f"Zakończono pobieranie strony {page_number}, Pobranych i zapisanych otwartych przetargów: {total_notices_downloaded}")

async def fetch_notice_details(session: aiohttp.ClientSession, notice_url: str):
    """Pobiera pojedyńcze ogłoszenie"""
    async with session.get(notice_url, headers={
        "Accept-Language": "pl,pl-PL;q=0.9"
    }) as response:
        if response.status != 200:
            print(f"Błąd HTTP {response.status} dla ogłoszenia {notice_url.split('/')[-1]}")
            return ""

        try:
            return await response.text()
        except Exception as e:
            print(f"Błąd parsowania tekstu ogłoszenia {notice_url.split('/')[-1]}: {e}")
            return ""


async def process_notice_details(session: aiohttp.ClientSession, notice_url: str) -> dict:
    data = await fetch_notice_details(session, notice_url)
    notice_doc = BeautifulSoup(data, "html.parser")

    li_list = notice_doc.find_all("li", "proceeding-info-list-item")
    for li in li_list:
        if li.div.text == "Organizacja":
            try:
                organisation: str = li.contents[0].text
            except AttributeError as e:
                print(notice_url)
                raise e
            break
    else:
        organisation: str = "Nie podano nazwy"

    requirements = notice_doc.find("div", { "id": "requirements" })
    description: str = requirements.text.strip() if requirements and requirements.text and requirements.text.strip() else "bez opisu"

    attachment_url_list: list[str] = []
    attachments_table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    if attachments_table:
        table_rows = attachments_table.tbody.find_all("tr")
        for row in table_rows:
            attachment_url_list.append(row.find("a", "proceeding-file-download")['href'][2:])

    return {
        "client_name": organisation,
        "description": description,
        "attachments": attachment_url_list
    }

async def get_pages_number(session: aiohttp.ClientSession):
    """Pobiera informację o ilości stron paginacji, na jakich są ogłoszenia"""
    data = await fetch_page(session, 1)

    doc = BeautifulSoup(data, "html.parser")
    ul = doc.find("ul", "pagination")
    li_list = ul.find_all("li")
    total_pages:int = int(li_list[-2].a.text)
    return total_pages

async def main():
    setup_directories()

    print("Inicjalizacja scrapera HTML platformazakupowa.pl...")

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    async with aiohttp.ClientSession(connector=connector) as session:
        total_pages: int = await get_pages_number(session)
        tasks = [process_page(session, page_number) for page_number in range(1, total_pages+1)]
        await asyncio.gather(*tasks)

    print("Zakończono.")


if __name__ == "__main__":
    asyncio.run(main())