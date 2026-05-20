import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import movie_service
import importlib
importlib.reload(movie_service)

url = 'https://episodes.modpro.blog/archives/9131'
print(f"Testing Playwright resolver on: {url}")
res = movie_service._resolve_gadgetsweb_playwright(url)
print(f"Result: {res}")
