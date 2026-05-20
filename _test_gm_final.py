import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

os.environ["SCRAPER_API_KEY"] = ""
import movie_service
movie_service.SCRAPER_API_KEY = ""

from movie_service import _resolve_gadgetsweb, _scrape_hblinks_page, _get

print("="*60)
print("Testing: https://greenmountmotors.com/homelander/")
print("="*60)

# This URL doesn't have ?id= so it'll go to Playwright fallback
# But let's check what's on this page first
import requests, re
from bs4 import BeautifulSoup

resp = requests.get("https://greenmountmotors.com/homelander/", 
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
                    timeout=15)

print(f"\nStatus: {resp.status_code}, Size: {len(resp.text)}")

# The /homelander/ page reads localStorage('o') and uses it
# It needs a prior ?id= visit to set localStorage
# But let's see what links/buttons are on it
soup = BeautifulSoup(resp.text, 'html.parser')

# Find the actual button/link that reveals after countdown
# Look for setAttribute("href",...) or window.open patterns
attr_hrefs = re.findall(r'setAttribute\(["\']href["\'],\s*["\']([^"\']+)["\']', resp.text)
window_opens = re.findall(r'window\.open\(["\']([^"\']+)["\']', resp.text)
window_locs = re.findall(r'window\.location(?:\.href)?\s*=\s*["\']([^"\']+)["\']', resp.text)

print(f"\nsetAttribute hrefs: {attr_hrefs}")
print(f"window.open: {window_opens}")  
print(f"window.location: {window_locs}")

# Look for the link element that gets the href set
link_els = soup.find_all('a', id=True)
print(f"\nNamed links: {[(a.get('id'), a.get('href','')[:50]) for a in link_els]}")

# The page likely reads from localStorage and sets it after countdown
# Let's look for the JS that handles the 'o' value
o_reads = re.findall(r"getItem\(['\"]o['\"]\)", resp.text)
print(f"\nlocalStorage.getItem('o') calls: {len(o_reads)}")

# Find the full JS decode logic
scripts = soup.find_all('script')
for i, s in enumerate(scripts):
    if s.string and 'getItem' in (s.string or ''):
        print(f"\nScript with getItem (script #{i}):")
        print(s.string[:800])
        print("...")

print("\n" + "="*60)
print("Now testing FULL FLOW: gadgetsweb -> greenmountmotors -> hblinks")
print("="*60)

# Use Episode 1 gadgetsweb URL from Dutton Ranch
gw_url = "https://gadgetsweb.xyz/?id=dnNpaWNMR0RveHpWZGgzRzhwYUdpaHJQMEl2ejZVTU5rb3hJQXV6Q0ZTNUdVUW1VVnlUYmQyc0VGOGZMa0p4dUIwVWFsV25zOTZXNDBFZGhQZEF2cGg3eEttQWtCNkluL253cmtiMEorTGs9"

print(f"\nResolving: {gw_url[:60]}...")
links = _resolve_gadgetsweb(gw_url)

print(f"\nFinal links resolved: {len(links)}")
for i, lk in enumerate(links):
    print(f"  {i+1}. [{lk['label'][:35]}] -> {lk['url'][:70]}")

print("\nDone!")
