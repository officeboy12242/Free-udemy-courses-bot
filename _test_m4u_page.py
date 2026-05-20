import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup

url = 'https://movies4u.ee/krishnavataram-part-1-the-heart-hridayam-2026-hdtc-hindi-line-full-movie-480p-720p-1080p/'
print(f"Fetching: {url}\n")

resp = requests.get(url, verify=False, timeout=25)
soup = BeautifulSoup(resp.text, 'html.parser')

# Find all links
links = soup.find_all('a', href=True)
print(f"Total links: {len(links)}\n")

# Find mdrive or download links
download_links = []
for a in links:
    href = a.get('href', '')
    text = a.get_text(strip=True)
    
    if 'mdrive' in href.lower() or 'mdisk' in href.lower() or 'g-direct' in text.lower():
        parent = a.find_parent(['p', 'div', 'h2', 'h3', 'h4'])
        parent_text = parent.get_text(strip=True)[:100] if parent else ''
        download_links.append((text, href, parent_text))

print(f"Found {len(download_links)} mdrive/download links:\n")
for text, href, parent in download_links[:15]:
    print(f"Text: {text}")
    print(f"URL: {href}")
    print(f"Parent: {parent}")
    print()
