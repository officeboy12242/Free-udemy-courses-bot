"""Quick test: Get messycloud links from The Walk (2015) on 1TamilMV."""
import time
import cloudscraper
from bs4 import BeautifulSoup

scraper = cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False})

def safe_get(url, retries=3, delay=3, **kwargs):
    for attempt in range(retries):
        try:
            return scraper.get(url, **kwargs)
        except Exception as e:
            if attempt < retries - 1:
                print(f'  Retry {attempt+1}/{retries} after error: {str(e)[:50]}...')
                time.sleep(delay * (attempt + 1))
            else:
                raise

url = 'https://www.1tamilmv.futbol/index.php?/forums/topic/198186-the-walk-2015-bluray-original-audios-1080p-720p-x264-dd-20-224kbps-tamil-telugu-hindi-eng-24gb-14gb-x264-tamil-telugu-hindi-450mb-esub/'
print('Fetching The Walk (2015) page...')
resp = safe_get(url, timeout=25)
soup = BeautifulSoup(resp.text, 'html.parser')
content = soup.select_one('article')
if not content:
    content = soup

links_found = []

for a in content.find_all('a', href=True):
    text = a.get_text(strip=True)
    href = a.get('href', '')
    if text == 'DIRECT LINK':
        # Get quality from previous sibling bold/strong text
        prev = a.find_previous('strong')
        quality_text = prev.get_text(strip=True) if prev else ''
        quality = 'Unknown'
        if '1080p' in quality_text:
            quality = '1080p'
        elif '720p' in quality_text:
            quality = '720p'
        elif '500MB' in quality_text or 'Rip' in quality_text:
            quality = '480p/Rip'

        print(f'\n[{quality}] Found DIRECT LINK: {href[:50]}...')
        print('  Step 1: Following cyberloom/l redirect...')
        time.sleep(2)
        r2 = safe_get(href, allow_redirects=True, timeout=15)
        soup2 = BeautifulSoup(r2.text, 'html.parser')
        
        continue_link = None
        for link in soup2.find_all('a', href=True):
            if '/out?' in link.get('href', ''):
                continue_link = link.get('href')
                break
        
        if continue_link:
            print(f'  Step 2: Following continue link...')
            time.sleep(2)
            r3 = safe_get(continue_link, allow_redirects=True, timeout=15)
            final_url = r3.url
            print(f'  FINAL MESSYCLOUD: {final_url}')
            links_found.append({'quality': quality, 'url': final_url})
        else:
            print('  No continue link found')

print('\n' + '=' * 80)
print('FINAL RESULTS - Messycloud Direct Download Links:')
print('=' * 80)
for i, link in enumerate(links_found, 1):
    print(f'{i}. [{link["quality"]}] {link["url"]}')
