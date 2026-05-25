import sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
os.environ['M4U_BASE_URL'] = 'https://movies4u.gr'

from movie_service import m4u_movie_links

print('=== TESTING MOVIE LINKS ===')
test_url = 'https://movies4u.gr/sathi-leelavathi-2026-web-dl-tamil-telugu/'
print(f'Fetching links for: {test_url}')
links = m4u_movie_links(test_url)
print(f'Found {len(links.get("links", []))} links')
for l in links.get('links', []):
    print(f"  {l['label']} -> {l['url']}")
