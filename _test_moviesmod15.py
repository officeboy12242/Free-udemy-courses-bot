import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import movie_service
import importlib
importlib.reload(movie_service)

url = 'https://cloud.unblockedgames.world/?sid=a3Y4azk3STZ5RVphb1c0d0pkeDllbjluV0NSTDRXNWlOSmJZTDFBU1RwM3AwTEJSbHhsejZLcmNYQzFsVGV2QkxMUmpsdURZR3hQNEo5c2g2UHhoMWRBNmt2dWQzZWx3ZjU1dkhTT3FySFR3bHlVZXhNQlg3TldtR0hkK3A4c21jWFVDaTVBQlRJeW1xUnVpZ2ZRdDRDc0R6bE0xZGlYNXg2WU5taDFvZkQ5SXBML2l2MWFQdlgyUlBBTzlOY0F6WGNEOTM5TmM3TDhxYjVVZmlHMG1HcFV5ZzlPS2xCWThMNitmUWFzaDBTWDBuMysxNGxYcUJMNEZBOEczUmc1dw=='
print(f"Testing Playwright resolver on: {url[:100]}...")
res = movie_service._resolve_gadgetsweb_playwright(url)
print(f"Result: {res}")
