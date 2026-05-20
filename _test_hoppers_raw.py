import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup
from movie_service import m4u_search, m4u_movie_links

print("="*70)
print("Hoppers - Raw Link Labels")
print("="*70)

results = m4u_search('hoppers', 1)
if results:
    movie = results[0]
    print(f"\nMovie: {movie['title']}")
    print(f"URL: {movie['url']}\n")
    
    # Get raw links
    data = m4u_movie_links(movie['url'])
    
    print("RAW LINKS:")
    print("-"*70)
    for i, lk in enumerate(data.get('links', []), 1):
        label = lk.get('label', 'N/A')
        name = lk.get('name', 'N/A')
        url = lk.get('url', 'N/A')[:60]
        print(f"\n[{i}] Label: {label}")
        print(f"    Name: {name}")
        print(f"    URL: {url}...")
else:
    print("No results")
