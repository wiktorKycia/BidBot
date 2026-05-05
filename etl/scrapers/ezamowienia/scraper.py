import requests
from datetime import datetime

url = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
date_format = "%Y-%m-%dT%H:%M:%S.%fZ"

data = requests.get(
    "https://ezamowienia.gov.pl/mo-board/api/v1/notice?NoticeType=ContractNotice&PublicationDateFrom=2026-05-05T00:00:00&PublicationDateTo=2026-12-31T23:59:59&PageSize=1",
    headers={
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
    }
)

notice = data.json()[0]

pubDate: datetime = datetime.fromisoformat(notice.get('publicationDate'))
print(pubDate)

html: str = notice.get('htmlBody')
with open("file.html", "w") as f:
    f.write(html)