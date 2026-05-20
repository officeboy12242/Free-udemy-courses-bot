import requests, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
from bs4 import BeautifulSoup
load_dotenv()

key = os.getenv('SCRAPER_API_KEY')
url = 'https://new1.hdhub4u.limo/'

print(f'Testing HDHub4u with ScraperAPI...')
params = {'api_key': key, 'url': url, 'render': 'false'}
resp = requests.get('http://api.scraperapi.com', params=params, timeout=30)

print(f'Status: {resp.status_code}, Length: {len(resp.text)}')

soup = BeautifulSoup(resp.text, 'html.parser')

# Check the selector used by hdhub_latest_movies
lis = soup.select('ul.recent-movies li.thumb')
print(f'Found {len(lis)} li.thumb elements')

# Show first few titles
for i, li in enumerate(lis[:3]):
    fig = li.find('figcaption')
    a = li.find('a', href=True)
    if fig and a:
        title = fig.get_text(strip=True)
        href = a['href']
        print(f'  {i+1}. {title[:60]}... ({href[:50]}...)')

print(f'\nSuccess! Found {len(lis)} movies.')
