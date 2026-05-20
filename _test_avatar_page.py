import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup

url = 'https://moviesmod.farm/download-avatar-fire-and-ash-2025-hindi-english-480p-720p-1080p/'
print(f"Fetching: {url}\n")

resp = requests.get(url, verify=False, timeout=25)
soup = BeautifulSoup(resp.text, 'html.parser')

# Find all links
links = soup.find_all('a', href=True)
print(f"Total links found: {len(links)}\n")

# Find modpro links
modpro = [a for a in links if 'episodes.modpro.blog' in a['href']]
print(f"Modpro links found: {len(modpro)}\n")

for a in modpro:
    text = a.get_text(strip=True)
    href = a['href']
    print(f"Text: {text}")
    print(f"URL: {href}")
    print()

# Check h2 and h3 headers
print("\n--- Headers ---")
headers = soup.find_all(['h2', 'h3'])
for h in headers:
    print(f"{h.name}: {h.get_text(strip=True)}")
