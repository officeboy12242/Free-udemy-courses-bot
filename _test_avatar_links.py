import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup

url = 'https://moviesmod.farm/download-avatar-fire-and-ash-2025-hindi-english-480p-720p-1080p/'
print(f"Fetching: {url}\n")

resp = requests.get(url, verify=False, timeout=25)
soup = BeautifulSoup(resp.text, 'html.parser')

# Find all links with their parent context
links = soup.find_all('a', href=True)

# Filter for download-related links
download_links = []
for a in links:
    text = a.get_text(strip=True).lower()
    href = a['href']
    # Look for quality indicators or download text
    if any(q in text for q in ['480p', '720p', '1080p', 'download', 'episode']) or \
       'modpro.blog' in href or 'uhdmovies' in href or 'driveseed' in href:
        parent = a.find_parent(['h2', 'h3', 'p', 'div'])
        parent_text = parent.get_text(strip=True)[:100] if parent else 'N/A'
        download_links.append((text, href, parent_text))

print(f"Download-related links: {len(download_links)}\n")
for text, href, parent in download_links[:30]:
    print(f"Text: {text}")
    print(f"URL: {href}")
    print(f"Parent: {parent}")
    print()
