import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- MOVIE LINKS ---")
# Look for links that might be movies
movie_links = soup.find_all('a', href=True)
for l in movie_links:
    href = l.get('href')
    # Maybe they use post.php?id= or similar
    if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href and href != '#' and href != 'https://top.xdmovies.wtf':
        print(f"{href} -> {l.get_text(strip=True)[:30]}")

print("\n--- CARDS ---")
cards = soup.select('.card, .movie, .item, .post')
for c in cards[:5]:
    print(c.prettify()[:200])
