import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from playwright.sync_api import sync_playwright

url = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"

try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=15000)
        page.wait_for_timeout(5000)
        html = page.content()
        browser.close()
        
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, 'html.parser')
        links = soup.find_all('a', href=True)
        for l in links:
            href = l.get('href')
            if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
                print(f"{l.get_text(strip=True)} -> {href}")
except Exception as e:
    print(f"Error: {e}")
