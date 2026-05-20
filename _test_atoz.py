import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup
import json

url = "https://atoz.cinemaz.workers.dev/"
r = requests.get(url, impersonate="chrome", timeout=15)
print(f"Status: {r.status_code}")

soup = BeautifulSoup(r.text, 'html.parser')
cards = soup.find_all('a', href=True)

print("--- MOVIE LINKS ---")
movie_links = []
for c in cards:
    href = c.get('href')
    if href and ('/movie/' in href or '/tv/' in href):
        movie_links.append(href)
        print(f"{c.get_text(strip=True)[:30]} -> {href}")

# Let's check the first movie link to see what the download page looks like
if movie_links:
    movie_url = "https://atoz.cinemaz.workers.dev" + movie_links[0]
    print(f"\n--- FETCHING MOVIE PAGE: {movie_url} ---")
    r2 = requests.get(movie_url, impersonate="chrome", timeout=15)
    print(f"Status: {r2.status_code}")
    
    soup2 = BeautifulSoup(r2.text, 'html.parser')
    
    # Look for download buttons/links
    print("\n--- DOWNLOAD LINKS ---")
    for a in soup2.find_all('a', href=True):
        href = a.get('href')
        text = a.get_text(strip=True).lower()
        if 'download' in text or 'watch' in text or 'http' in href:
            print(f"{text} -> {href}")
            
    # Look for scripts that might contain the links
    print("\n--- SCRIPTS ---")
    for s in soup2.find_all('script'):
        if s.string and ('drive' in s.string.lower() or 'link' in s.string.lower() or 'http' in s.string.lower()):
            print(s.string[:500])
