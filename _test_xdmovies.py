import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/"
print(f"Fetching {url}")
try:
    r = requests.get(url, impersonate="chrome", timeout=15)
    print(f"Status: {r.status_code}")
    soup = BeautifulSoup(r.text, 'html.parser')
    
    # Try to find movie items
    items = soup.select("a[href*='/movie/'], a[href*='/show/']")
    if not items:
        # Maybe different structure
        items = soup.select("div.post, article.post, div.item")
        
    print(f"Found {len(items)} potential items")
    for item in items[:5]:
        if item.name == 'a':
            print(f"Link: {item.get('href')}")
            print(f"Text: {item.get_text(strip=True)}")
        else:
            a = item.find('a')
            print(f"Link: {a.get('href') if a else 'No link'}")
            print(f"Text: {item.get_text(strip=True)[:50]}")
            
except Exception as e:
    print(f"Error: {e}")
