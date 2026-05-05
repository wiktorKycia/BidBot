import os

from etl.scrapers.ezamowienia.scraper import scrape

if __name__ == '__main__':
    if not os.path.exists("data"):
        os.mkdir("data")
    scrape("data")
