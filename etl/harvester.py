import os

from scrapers.ezamowienia.scraper import scrape

if not os.path.exists("data"):
    os.mkdir("data")
scrape("data")