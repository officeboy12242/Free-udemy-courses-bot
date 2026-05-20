import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import movie_service
import importlib
importlib.reload(movie_service)

url = 'https://moviesmod.farm/download-the-boys-season-1-5-hindi-480p-720p-1080p/'
print(f"Scraping {url}...")
res = movie_service.moviesmod_movie_links(url)

print(f"Title: {res['title']}")
for link in res['links']:
    print(f"[{link['label']}]")
    print(f"  {link['url']}")
