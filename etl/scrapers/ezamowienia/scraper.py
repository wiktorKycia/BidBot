import json
import os

import requests
from datetime import datetime, timezone
from etl.scrapers.ezamowienia.models import notice_types, response_attributes

url = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
date_format = "%Y-%m-%dT%H:%M:%S.%fZ"

def scrape(output_folder_path: str):
    for noticeType in notice_types:
        start_date = datetime.fromtimestamp(0)
        print(start_date, datetime.now())

        while start_date < datetime.now(timezone.utc):
            response = requests.get(
                f"{url}?NoticeType={noticeType}&PublicationDateFrom={start_date.isoformat()}&PublicationDateTo={datetime.now(timezone.utc).isoformat()}&PageSize=500",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
                }
            )

            data = response.json()

            if not data: break

            for obj in data:
                if not os.path.exists(os.path.join(output_folder_path, "raw")):
                    os.mkdir(os.path.join(output_folder_path, "raw"))
                with open(os.path.join(output_folder_path, "raw", obj.get('objectId')), "w") as f:
                    json.dump(obj, f)

            start_date = datetime.fromisoformat(data[-1].get('publicationDate')).replace('Z', '+00:00')


