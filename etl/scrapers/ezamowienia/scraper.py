import json
import os

import requests
from datetime import datetime, timezone
from etl.scrapers.ezamowienia.models import notice_types, response_attributes

url = "https://ezamowienia.gov.pl/mo-board/api/v1/notice"
date_format = "%Y-%m-%dT%H:%M:%S"

def scrape(output_folder_path: str):
    for noticeType in notice_types:
        start_date = datetime.fromtimestamp(0)

        while start_date.astimezone() < datetime.now().astimezone():
            response = requests.get(
                f"{url}?NoticeType={noticeType}&PublicationDateFrom={start_date.strftime(date_format)}&PublicationDateTo={datetime.now().strftime(date_format)}&PageSize=500",
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0"
                }
            )

            data = response.json()
            if 'error' in data:
                print(data['error'])
                break

            if len(data) == 0: break

            for i, obj in enumerate(data):
                if not os.path.exists(os.path.join(output_folder_path, "raw")):
                    os.mkdir(os.path.join(output_folder_path, "raw"))
                with open(os.path.join(output_folder_path, "raw", f"{i:03d}. {obj.get('objectId')}"), "w") as f:
                    json.dump(obj, f)

            pub = data[-1].get('publicationDate')
            if pub:
                start_date = datetime.fromisoformat(pub)
                print(start_date)
            else:
                break

        print(f"all {noticeType} fetched")