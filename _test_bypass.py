"""Test bypassing stackmint.ink / inkvoyage.xyz to get messycloud.ink links."""
import os
import sys
import re
import time
import base64
import cloudscraper
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
)

TAMILMV_BASE = "https://www.1tamilmv.futbol"

# Step 1: Get the cyberloom link from a 1tamilmv movie page
test_url = "https://www.cyberloom.best/l/9OBM0iGa"

print("=" * 80)
print("Step 1: Following cyberloom redirect...")
print("=" * 80)

resp = scraper.get(test_url, allow_redirects=True, timeout=15)
intermediate_url = resp.url
print(f"Intermediate URL: {intermediate_url}")
print(f"Status: {resp.status_code}")
print(f"Final URL after redirects: {resp.url}")

print("\n" + "=" * 80)
print("Step 2: Analyzing the intermediate page HTML...")
print("=" * 80)

# Get the page HTML
html = resp.text
print(f"Page length: {len(html)} chars")

# Look for go-link form (common pattern in link shorteners)
soup = BeautifulSoup(html, 'html.parser')

# Check for go-link form
go_link_form = soup.find(id="go-link")
if go_link_form:
    print("[FOUND] go-link form!")
    inputs = go_link_form.find_all('input')
    data = {inp.get('name'): inp.get('value') for inp in inputs if inp.get('name')}
    print(f"Form data: {data}")
else:
    print("[NOT FOUND] No go-link form")

# Check for any form on the page
all_forms = soup.find_all('form')
print(f"\nTotal forms on page: {len(all_forms)}")
for i, form in enumerate(all_forms):
    print(f"  Form {i+1}: id={form.get('id')}, action={form.get('action')}, method={form.get('method')}")
    form_inputs = form.find_all('input')
    for inp in form_inputs:
        print(f"    Input: name={inp.get('name')}, value={inp.get('value', '')[:50]}, type={inp.get('type')}")

# Look for JavaScript with URLs or base64 encoded data
scripts = soup.find_all('script')
print(f"\nTotal scripts on page: {len(scripts)}")
for i, script in enumerate(scripts):
    script_text = script.string or ""
    if script_text:
        # Look for URLs in script
        urls_found = re.findall(r'https?://[^\s\'"<>]+', script_text)
        if urls_found:
            print(f"\n  Script {i+1} contains URLs:")
            for url in urls_found[:5]:
                print(f"    {url[:80]}")
        
        # Look for base64 strings
        b64_matches = re.findall(r'[A-Za-z0-9+/]{20,}={0,2}', script_text)
        if b64_matches:
            print(f"\n  Script {i+1} may contain base64 ({len(b64_matches)} matches):")
            for b64 in b64_matches[:3]:
                try:
                    decoded = base64.b64decode(b64).decode('utf-8', errors='ignore')
                    if 'http' in decoded or 'messy' in decoded:
                        print(f"    DECODED: {decoded[:80]}")
                except:
                    pass
        
        # Look for window.location or redirect patterns
        if 'window.location' in script_text or 'href' in script_text:
            location_matches = re.findall(r'(?:window\.location|location\.href|window\.open)\s*[=\(]\s*[\'"]([^\'"]+)[\'"]', script_text)
            if location_matches:
                print(f"\n  Script {i+1} redirects to:")
                for loc in location_matches:
                    print(f"    {loc[:80]}")
        
        # Look for timer/countdown patterns
        if 'setTimeout' in script_text or 'setInterval' in script_text:
            print(f"\n  Script {i+1} has timer/countdown")
            
        # Print first 200 chars of script for debugging
        if len(script_text) > 50:
            print(f"\n  Script {i+1} content preview:")
            print(f"    {script_text[:300]}...")

# Check for links with messy/cloud in them
print("\n" + "=" * 80)
print("Step 3: Looking for links/buttons...")
print("=" * 80)

all_links = soup.find_all('a', href=True)
print(f"Total links: {len(all_links)}")
for link in all_links:
    href = link.get('href', '')
    text = link.get_text(strip=True)
    if href and href != '#' and not href.startswith('javascript:'):
        print(f"  {text[:30]:30s} -> {href[:60]}")

# Check for buttons
all_buttons = soup.find_all('button')
print(f"\nTotal buttons: {len(all_buttons)}")
for btn in all_buttons:
    print(f"  id={btn.get('id')}, class={btn.get('class')}, text={btn.get_text(strip=True)[:30]}")

print("\n" + "=" * 80)
print("Step 4: Try the go-link POST bypass pattern...")
print("=" * 80)

# Parse the domain from intermediate URL
parsed = re.match(r'(https?://[^/]+)', intermediate_url)
domain = parsed.group(1) if parsed else intermediate_url

# Try the standard bypass: wait + POST to /links/go
if go_link_form:
    data = {inp.get('name'): inp.get('value') for inp in go_link_form.find_all('input') if inp.get('name')}
    print(f"Waiting 10 seconds (bypassing timer)...")
    time.sleep(10)
    
    headers = {"x-requested-with": "XMLHttpRequest"}
    post_url = f"{domain}/links/go"
    print(f"POSTing to: {post_url}")
    print(f"Data: {data}")
    
    post_resp = scraper.post(post_url, data=data, headers=headers)
    print(f"Response status: {post_resp.status_code}")
    print(f"Response text: {post_resp.text[:500]}")
    
    try:
        json_resp = post_resp.json()
        print(f"JSON response: {json_resp}")
        if 'url' in json_resp:
            print(f"\n*** FINAL LINK: {json_resp['url']} ***")
    except:
        print("Not JSON response")
else:
    print("No go-link form found, trying alternative approach...")
    
    # Try common API endpoints
    endpoints = ['/links/go', '/api/getlink', '/go', '/get-link']
    for ep in endpoints:
        try:
            test_resp = scraper.get(f"{domain}{ep}", timeout=10)
            print(f"  GET {domain}{ep} -> {test_resp.status_code}")
        except Exception as e:
            print(f"  GET {domain}{ep} -> Error: {e}")
