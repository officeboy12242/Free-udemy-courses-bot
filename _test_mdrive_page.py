import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup

url = 'https://mdrive.ink/mdisk/88785'
print(f"Fetching: {url}\n")

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
})

resp = session.get(url, verify=False, timeout=20)
print(f"Status: {resp.status_code}")
print(f"URL: {resp.url}")
print(f"\n{'='*60}")
print("HTML Content (first 3000 chars):")
print('='*60)
print(resp.text[:3000])
print("\n...")
print(f"\n{'='*60}")
print("All links on page:")
print('='*60)

soup = BeautifulSoup(resp.text, 'html.parser')
links = soup.find_all('a', href=True)
for a in links[:20]:
    text = a.get_text(strip=True)
    href = a['href']
    print(f"{text[:50]:<50} -> {href}")
