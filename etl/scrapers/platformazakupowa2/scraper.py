import json
import shutil
from pathlib import Path
from datetime import datetime, timezone

import aiofiles
import aiohttp
import asyncio
from aiohttp import TCPConnector
from aiohttp.resolver import AsyncResolver
from bs4 import BeautifulSoup

BASE_URL = "https://platformazakupowa.pl"
ALL_RESOURCES_URL = "https://platformazakupowa.pl/all"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw_html"
PARSED_DIR = DATA_DIR / "parsed"
ATTACHMENTS_DIR = DATA_DIR / "attachments"

SEMAPHORE = asyncio.Semaphore(15)


def setup_directories():
    for directory in [RAW_DIR, PARSED_DIR, ATTACHMENTS_DIR]:
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


async def download_file(session: aiohttp.ClientSession, url: str, output_dir: Path, filename: str):
    # if not filename:
    #     filename = Path(url).name
    filepath = output_dir / filename
    async with SEMAPHORE:
        async with session.get(url) as response:
            if response.status == 200:
                async with aiofiles.open(filepath, 'wb') as f:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        if chunk:
                            await f.write(chunk)
                # print(f"Downloaded {filename}")
            else:
                print(f"Failed to download {filename}: {response.status}")


async def fetch_page(session: aiohttp.ClientSession, page_number: int) -> str:
    params = {"page": page_number, "limit": 100}
    async with SEMAPHORE:
        async with session.get(ALL_RESOURCES_URL, params=params, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }) as response:
            if response.status != 200:
                print(f"Blad HTTP {response.status} dla strony {page_number}")
                return ""
            try:
                return await response.text()
            except Exception as e:
                print(f"Blad parsowania tekstu strony {page_number}: {e}")
                return ""


async def fetch_notice_details(session: aiohttp.ClientSession, notice_url: str):
    try:
        async with SEMAPHORE:
            async with session.get(notice_url, headers={"Accept-Language": "pl,pl-PL;q=0.9"}) as response:
                if response.status != 200:
                    print(f"Błąd HTTP {response.status} dla przetargu {notice_url.split('/')[-1]}")
                    return ""
                try:
                    return await response.text()
                except Exception as e:
                    print(f"Błąd:  {notice_url.split('/')[-1]}: {e}")
                    return ""
    except asyncio.TimeoutError:
        print("Timeout")
        return ""


async def process_notice_details(session: aiohttp.ClientSession, notice_url: str, notice_id: str) -> dict:
    data = await fetch_notice_details(session, notice_url)
    if not data:
        return {
            "client_name": None,
            "publication_date": None,
            "description": None,
            "raw_html": None,
            "attachments": []
        }

    notice_doc = BeautifulSoup(data, "lxml")
    organisation = "Nie podano nazwy"
    publication_date = None

    li_list = notice_doc.find_all("li", class_="proceeding-info-list-item")
    for li in li_list:
        div = li.find("div")
        if not div:
            continue

        label_text = div.text.strip()

        if "Organizacja" in label_text:
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                organisation = " ".join(texts[1:])

        elif "Opublikowano" in label_text or "Zamieszczenia" in label_text:
            texts = list(li.stripped_strings)
            if len(texts) > 1:
                raw_date = " ".join(texts[1:]).strip()
                try:
                    clean_date_str = " ".join(raw_date.split()[:2])
                    publication_date = datetime.strptime(clean_date_str, '%Y-%m-%d %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%SZ')
                except Exception as e:
                    print(f"Blad parsowania daty publikacji: {raw_date}")
                    publication_date = raw_date

    requirements = notice_doc.find("div", {"id": "requirements"})
    description = requirements.text.strip() if requirements and requirements.text.strip() else "bez opisu"

    attachments_list = []
    attachments_table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    if attachments_table:
        table_rows = attachments_table.tbody.find_all("tr")
        for row in table_rows:
            a_tag = row.find("a", class_="proceeding-file-download")
            if a_tag and 'href' in a_tag.attrs:
                # filename
                td_with_filename = row.find("td", class_="text-left")
                filename = td_with_filename.text.strip()
                if filename.split('.')[-1] in ['zip', '7z']:
                    filename = None

                # file url
                href = a_tag['href']
                if href.startswith(".."):
                    href = href[2:]

                attachments_list.append({
                    "url": f"https:{href}" if not href.startswith("http") else href,
                    "filename": filename
                })

    tasks_to_download = []
    # print(attachments_list)
    for attachment in attachments_list:
        if not attachment.get('filename'):
            continue
        directory = ATTACHMENTS_DIR / notice_id
        directory.mkdir(parents=True, exist_ok=True)
        tasks_to_download.append(download_file(session, attachment.get('url'), directory, attachment.get('filename')))

    if tasks_to_download:
        await asyncio.gather(*tasks_to_download)

    return {
        "client_name": organisation,
        "publication_date": publication_date,
        "description": description,
        "raw_html": notice_doc.prettify(),
        "attachments": attachments_list
    }


async def process_page(session: aiohttp.ClientSession, page_number: int) -> int:
    print(f"Rozpoczęto stronę {page_number}...")
    total_new_downloads = 0
    data = await fetch_page(session, page_number)

    if not data:
        return 0

    doc = BeautifulSoup(data, "lxml")
    notices = doc.find_all("div", class_="product-info")
    tasks_to_download = []

    async def process_single_notice(notice):
        nonlocal total_new_downloads
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

        details = await process_notice_details(session, notice_url, notice_id)

        parsed_data = {
            "id": notice_id,
            "source": "platformazakupowa.pl",
            "url": notice_url,
            "title": notice_name,
            "publication_date": details.get("publication_date"),
            "submitting_offers_date": submitting_offers_date,
            "client_name": details.get("client_name"),
            "description": details.get("description"),
            "attachments": details.get("attachments")
        }

        if details.get('raw_html'):
            raw_filepath = RAW_DIR / f"{notice_id}.html"
            await save_html(raw_filepath, details['raw_html'])

            parsed_filepath = PARSED_DIR / f"{notice_id}.json"
            await save_json(parsed_filepath, parsed_data)

            total_new_downloads += 1

    for notice in notices:
        tasks_to_download.append(process_single_notice(notice))

    if tasks_to_download:
        await asyncio.gather(*tasks_to_download)

    print(f"Zakończono stronę {page_number}. Zapisano nowych przetargów: {total_new_downloads}")
    return total_new_downloads


async def get_pages_number(session: aiohttp.ClientSession):
    data = await fetch_page(session, 1)
    if not data:
        raise Exception("Strona platformazakupowa.pl niedostępna")

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
    connector = TCPConnector(resolver=resolver, limit=30)
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        total_pages = await get_pages_number(session)
        print(f"Znaleziono stron: {total_pages}")

        total_all_new = 0
        for page_number in range(1, total_pages + 1):
            new_notices_count = await process_page(session, page_number)
            total_all_new += new_notices_count

            if new_notices_count == 0:
                print(f"Przerwano na stronie {page_number} - brak nowych danych. Uznano za zaktualizowane.")
                break

    print(f"Zakonczono. Łącznie nowych przetargow: {total_all_new}")


if __name__ == "__main__":
    asyncio.run(main())