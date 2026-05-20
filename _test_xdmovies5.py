import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests
from bs4 import BeautifulSoup

url = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"
r = requests.get(url, impersonate="chrome", timeout=15)
soup = BeautifulSoup(r.text, 'html.parser')

print("--- DOWNLOAD LINKS ---")
links = soup.find_all('a', href=True)
for l in links:
    href = l.get('href')
    text = l.get_text(strip=True)
    if 'category' not in href and 'discord' not in href and 'xdmovies.com' not in href:
        print(f"{text} -> {href}")

print("\n--- BUTTONS ---")
buttons = soup.find_all('button')
for b in buttons:
    print(b.get_text(strip=True))
    
print("\n--- HTML SNIPPET ---")
dl_section = soup.find(lambda tag: tag.name in ['div', 'section'] and 'download' in tag.get('class', [''])[0].lower())
if dl_section:
    print(dl_section.prettify()[:1000])
else:
    # Just print the middle of the body
    body = soup.find('body')
    if body:
        print(body.prettify()[2000:3000])
