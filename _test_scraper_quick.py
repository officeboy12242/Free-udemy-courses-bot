import requests, os, sys
sys.stdout.reconfigure(encoding='utf-8')
from dotenv import load_dotenv
load_dotenv()

key = os.getenv('SCRAPER_API_KEY')
print(f'Key: {key[:10]}...')

test_urls = [
    ("HDHub4u", "https://new1.hdhub4u.limo/"),
    ("4KHDHub", "https://4khdhub.link/category/hindi-movies/"),
    ("Movies4U", "https://movies4u.ee/"),
]

for name, url in test_urls:
    print(f'\n{name}: {url}')
    params = {'api_key': key, 'url': url, 'render': 'false'}
    try:
        resp = requests.get('http://api.scraperapi.com', params=params, timeout=30)
        print(f'  Status: {resp.status_code}, Length: {len(resp.text)}')
        has_article = 'article' in resp.text.lower()
        print(f'  Has article tag: {has_article}')
        if len(resp.text) > 5000 and has_article:
            print(f'  SUCCESS: Got real content')
        else:
            print(f'  WARNING: Might be blocked or need render=true')
    except Exception as e:
        print(f'  ERROR: {type(e).__name__}: {str(e)[:80]}')

print('\nDone!')
