import sys
sys.stdout.reconfigure(encoding='utf-8')
import os
from dotenv import load_dotenv
load_dotenv()

SCRAPER_API_KEY = os.getenv("SCRAPER_API_KEY")
SCRAPER_API_URL = "http://api.scraperapi.com"

if not SCRAPER_API_KEY:
    print("No SCRAPER_API_KEY found in .env")
    print("These sites need ScraperAPI to work on cloud deployments:")
    print("  - HDHub4u (new1.hdhub4u.limo)")
    print("  - 4KHDHub (4khdhub.link)")
    print("  - Movies4U (movies4u.ee)")
    print("\nSet SCRAPER_API_KEY in .env or on Render dashboard.")
else:
    print(f"SCRAPER_API_KEY found: {SCRAPER_API_KEY[:10]}...")
    print("\nTesting with ScraperAPI...")

    import requests
    from bs4 import BeautifulSoup

    test_urls = [
        ("HDHub4u", "https://new1.hdhub4u.limo/"),
        ("4KHDHub", "https://4khdhub.link/category/hindi-movies/"),
        ("Movies4U", "https://movies4u.ee/"),
    ]

    for name, url in test_urls:
        print(f"\n{name}: {url}")
        try:
            params = {"api_key": SCRAPER_API_KEY, "url": url, "render": "true"}
            resp = requests.get(SCRAPER_API_URL, params=params, timeout=60)
            print(f"  Status: {resp.status_code}")
            print(f"  Content length: {len(resp.text)}")

            # Check if we got real content
            if len(resp.text) > 5000 and "article" in resp.text:
                soup = BeautifulSoup(resp.text, "html.parser")
                articles = soup.select("article")
                print(f"  Articles found: {len(articles)}")
                if articles:
                    title = articles[0].get_text(strip=True)[:50]
                    print(f"  First article: {title}...")
            else:
                print(f"  Warning: Content seems minimal or blocked")
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {str(e)[:100]}")
