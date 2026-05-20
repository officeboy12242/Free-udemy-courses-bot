import sys, os
sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright

url = 'https://cloud.unblockedgames.world/?sid=a3Y4azk3STZ5RVphb1c0d0pkeDllbjluV0NSTDRXNWlOSmJZTDFBU1RwM3AwTEJSbHhsejZLcmNYQzFsVGV2QkxMUmpsdURZR3hQNEo5c2g2UHhoMWRBNmt2dWQzZWx3ZjU1dkhTT3FySFR3bHlVZXhNQlg3TldtR0hkK3A4c21jWFVDaTVBQlRJeW1xUnVpZ2ZRdDRDc0R6bE0xZGlYNXg2WU5taDFvZkQ5SXBML2l2MWFQdlgyUlBBTzlOY0F6WGNEOTM5TmM3TDhxYjVVZmlHMG1HcFV5ZzlPS2xCWThMNitmUWFzaDBTWDBuMysxNGxYcUJMNEZBOEczUmc1dw=='

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()

    print(f"Navigating to {url[:100]}...")
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    
    for i in range(10):
        page.wait_for_timeout(2000)
        print(f"[{i*2}s] URL: {page.url}")
        
    browser.close()
