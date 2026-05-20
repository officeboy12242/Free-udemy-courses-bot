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
    page.wait_for_timeout(3000)

    for round_idx in range(6):
        print(f"\n--- Round {round_idx} ---")
        print(f"URL: {page.url}")
        
        btns = page.query_selector_all('a, button, [id*="verify"], [id*="continue"], [id*="download"]')
        print(f"Found {len(btns)} potential buttons")
        clicked = False
        for btn in btns:
            if btn.is_visible():
                text = (btn.text_content() or "").strip().lower()
                eid = btn.get_attribute('id') or ""
                cls = btn.get_attribute('class') or ""
                if 'verify' in text or 'continue' in text or 'download' in text or 'verify' in eid or 'continue' in eid or 'download' in eid:
                    print(f"Clicking button: text='{text}', id='{eid}', class='{cls}'")
                    btn.click()
                    clicked = True
                    break
        
        if not clicked:
            print("No button clicked.")
        
        page.wait_for_timeout(2000)
        
        has_countdown = page.evaluate('''() => {
            const selectors = ['#countdown', '[class*="countdown"]', '[class*="timer"]',
                               '[id*="countdown"]', '[id*="timer"]', '.loader'];
            for (const sel of selectors) {
                const el = document.querySelector(sel);
                if (el && el.offsetParent !== null) return true;
            }
            return false;
        }''')
        print(f"Has countdown: {has_countdown}")
        if has_countdown:
            page.wait_for_timeout(15000)
            
    browser.close()
