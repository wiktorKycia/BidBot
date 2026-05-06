import asyncio
import json
import shutil
from pathlib import Path

import aiofiles
import aiohttp
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup
from datetime import datetime, timezone

BASE_URL = "https://platformazakupowa.pl"
ALL_RESOURCES_URL = "https://platformazakupowa.pl/all"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw_html"
PARSED_DIR = DATA_DIR / "parsed"

SEMAPHORE = asyncio.Semaphore(100)


def setup_directories():
    for directory in [RAW_DIR, PARSED_DIR]:
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)


def is_downloaded(notice_id: str) -> bool:
    return (RAW_DIR / f"{notice_id}.html").exists()


def is_tender_open(deadline_str: str) -> bool:
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
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))


async def save_html(filepath: Path, data: str):
    async with aiofiles.open(filepath, mode="w", encoding="utf-8") as f:
        await f.write(data)


async def fetch_page(session: aiohttp.ClientSession, page_number: int) -> str:
    params = {"page": page_number, "limit": 100}
    async with SEMAPHORE:
        async with session.get(ALL_RESOURCES_URL, params=params, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }) as response:
            if response.status != 200:
                print(f"Błąd HTTP {response.status} dla strony {page_number}")
                return ""
            try:
                return await response.text()
            except Exception as e:
                print(f"Błąd parsowania tekstu strony {page_number}: {e}")
                return ""


async def fetch_notice_details(session: aiohttp.ClientSession, notice_url: str):
    try:
        async with SEMAPHORE:
            async with session.get(notice_url, headers={"Accept-Language": "pl,pl-PL;q=0.9"}) as response:
                if response.status != 200:
                    print(f"Błąd HTTP {response.status} dla ogłoszenia {notice_url.split('/')[-1]}")
                    return ""
                try:
                    return await response.text()
                except Exception as e:
                    print(f"Błąd parsowania tekstu ogłoszenia {notice_url.split('/')[-1]}: {e}")
                    return ""
    except asyncio.TimeoutError:
        print("Czas oczekiwania na żądanie minął")
        return ""


async def process_notice_details(session: aiohttp.ClientSession, notice_url: str) -> dict:
    data = await fetch_notice_details(session, notice_url)
    if not data:
        return {
            "client_name": None,
            "description": None,
            "raw_html": None,
            "attachments": []
        }

    notice_doc = BeautifulSoup(data, "lxml")
    organisation: str = "Nie podano nazwy"

    li_list = notice_doc.find_all("li", class_="proceeding-info-list-item")
    for li in li_list:
        div = li.find("div")
        if div and "Organizacja" in div.text:
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                organisation = " ".join(texts[1:])
            break

    requirements = notice_doc.find("div", {"id": "requirements"})
    description: str = requirements.text.strip() if requirements and requirements.text.strip() else "bez opisu"

    attachment_url_list: list[str] = []
    attachments_table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    if attachments_table:
        table_rows = attachments_table.tbody.find_all("tr")
        for row in table_rows:
            a_tag = row.find("a", class_="proceeding-file-download")
            if a_tag and 'href' in a_tag.attrs:
                href = a_tag['href']
                if href.startswith(".."):
                    href = href[2:]
                attachment_url_list.append(f"{BASE_URL}{href}" if not href.startswith("http") else href)

    return {
        "client_name": organisation,
        "description": description,
        "raw_html": notice_doc.prettify(),
        "attachments": attachment_url_list
    }


async def process_page(session: aiohttp.ClientSession, page_number: int):
    print(f"Rozpoczęto pobieranie strony {page_number}")
    total_notices_downloaded: int = 0
    data = await fetch_page(session, page_number)

    if not data:
        return

    doc = BeautifulSoup(data, "lxml")
    notices = doc.find_all("div", class_="product-info")

    async def process_single_notice(notice):
        nonlocal total_notices_downloaded
        a_tag = notice.find("a")
        if not a_tag or 'href' not in a_tag.attrs:
            return

        notice_id = a_tag['href'].split('/')[-1]
        notice_name = a_tag.text.strip()
        notice_url = f"{BASE_URL}{a_tag['href']}"

        span = notice.find("span", class_="auction-time")
        if not span or not span.find('b') or 'title' not in span.b.attrs:
            return

        submitting_offers_date_str = " ".join(span.b['title'].split()[:2]).strip()
        submitting_offers_date = datetime.strptime(submitting_offers_date_str, '%d-%m-%Y %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%SZ')

        if not is_tender_open(submitting_offers_date):
            return
        if not notice_id or is_downloaded(notice_id):
            return

        details = await process_notice_details(session, notice_url)

        parsed_data = {
            "id": notice_id,
            "source": "platformazakupowa.pl",
            "url": notice_url,
            "title": notice_name,
            "publication_date": None,
            "submitting_offers_date": submitting_offers_date,
            "notice_number": "Unknown",
            **details
        }

        if parsed_data.get('raw_html'):
            raw_filepath = RAW_DIR / f"{notice_id}.html"
            await save_html(raw_filepath, parsed_data['raw_html'])

            parsed_filepath = PARSED_DIR / f"{notice_id}.json"
            del parsed_data['raw_html']
            await save_json(parsed_filepath, parsed_data)

            total_notices_downloaded += 1

    tasks = [process_single_notice(notice) for notice in notices]
    await asyncio.gather(*tasks)

    print(f"Zakończono stronę {page_number}. Zapisano otwartych przetargów: {total_notices_downloaded}")


async def get_pages_number(session: aiohttp.ClientSession):
    data = await fetch_page(session, 1)
    if not data:
        raise Exception("Strona platformazakupowa.pl jest niedostępna!")

    doc = BeautifulSoup(data, "lxml")
    ul = doc.find("ul", class_="pagination")
    if not ul:
        return 1

    li_list = ul.find_all("li")
    try:
        total_pages = int(li_list[-2].a.text)
        return total_pages
    except (IndexError, ValueError, AttributeError):
        return 1


async def main():
    print("Inicjalizacja scrapera HTML platformazakupowa.pl...")
    setup_directories()

    resolver = AsyncResolver(nameservers=["1.1.1.1", "8.8.8.8"])
    connector = TCPConnector(resolver=resolver)
    timeout = aiohttp.ClientTimeout(total=90)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        total_pages: int = await get_pages_number(session)
        print(f"Znaleziono stron wyników do przetworzenia: {total_pages}")

        tasks = [process_page(session, page_number) for page_number in range(1, total_pages+1)]
        await asyncio.gather(*tasks)

    print("Zakończono pobieranie z platformazakupowa.pl.")

if __name__ == "__main__":
    asyncio.run(main())
