from bs4 import BeautifulSoup
import requests
from datetime import datetime

url = "https://platformazakupowa.pl/all?page=44&limit=100"
def fetch_notices():
    response = requests.get(url)

    doc = BeautifulSoup(response.text, "html.parser")

    notices = doc.find_all("div", "product-info")
    # print(notices)

    for notice in notices:
        notice_name = notice.a.text.strip()
        notice_url = f"https://platformazakupowa.pl{notice.a['href']}"
        span = notice.find("span", "auction-time")
        submitting_offers_date_str = " ".join(span.b['title'].split()[:2]).strip()
        submitting_offers_date = datetime.strptime(submitting_offers_date_str, '%d-%m-%Y %H:%M:%S').strftime('%Y-%m-%dT%H:%M:%SZ')
        print(f"\n{notice_name}: ")
        print(notice_url)
        print(submitting_offers_date)

def fetch_notice_details(notice_url: str) -> dict:
    resp = requests.get(notice_url, headers={
        "Accept-Language": "pl,pl-PL;q=0.9"
    })
    notice_doc = BeautifulSoup(resp.text, "html.parser")

    li_list = notice_doc.find_all("li", "proceeding-info-list-item")
    for li in li_list:
        if li.div.text == "Organizacja":
            organisation: str = li.find("a").text
            print(organisation)
            break
    else:
        organisation: str = "Nie podano nazwy"

    requirements = notice_doc.find("div", { "id": "requirements" })
    description: str = requirements.text

    attachment_url_list: list[str] = []
    attachments_table = notice_doc.find("table", {"id": "allAttachmentsTable"})
    table_rows = attachments_table.tbody.find_all("tr")
    for row in table_rows:
        attachment_url_list.append(row.find("a", "proceeding-file-download")['href'][2:])

    return {
        "client_name": organisation,
        "description": description,
        "attachments": attachment_url_list
    }


if __name__ == "__main__":
    print(fetch_notice_details("https://platformazakupowa.pl/transakcja/1294640"))