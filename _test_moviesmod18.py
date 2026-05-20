import sys, os
sys.stdout.reconfigure(encoding='utf-8')

from playwright.sync_api import sync_playwright

url = 'https://cloud.unblockedgames.world/t-mobile-is-providing-microsoft-surface-pro-9-users-with-30gb-of-complimentary-5g-data/'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(ignore_https_errors=True)
    page = context.new_page()

    print(f"Navigating to {url[:100]}...")
    page.goto(url, wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(3000)

    btns = page.query_selector_all('a, button')
    for btn in btns:
        if btn.is_visible():
            text = (btn.text_content() or "").strip()
            href = btn.get_attribute('href') or ""
            eid = btn.get_attribute('id') or ""
            cls = btn.get_attribute('class') or ""
            print(f"Visible: text='{text}', href='{href}', id='{eid}', class='{cls}'")
            
    browser.close()
