"""Test - follow the full redirect chain to get messycloud.ink link."""
import os
import sys
import re
import time
import cloudscraper
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

scraper = cloudscraper.create_scraper(
    browser={'browser': 'chrome', 'platform': 'windows', 'mobile': False}
)

# Start with cyberloom URL (from 1tamilmv "DIRECT LINK" button)
cyberloom_url = "https://www.cyberloom.best/l/9OBM0iGa"

print("STEP 1: cyberloom.best/l/ -> inkvoyage/stackmint")
print("-" * 60)
resp1 = scraper.get(cyberloom_url, allow_redirects=True, timeout=15)
intermediate_url = resp1.url
print(f"Got: {intermediate_url[:80]}")

# Parse the page to find "Continue to Destination" link
soup1 = BeautifulSoup(resp1.text, 'html.parser')
continue_link = None
for a in soup1.find_all('a', href=True):
    if 'continue' in a.get_text(strip=True).lower() or '/out?' in a.get('href', ''):
        continue_link = a.get('href')
        break

if not continue_link:
    # Try looking for the link in script
    for script in soup1.find_all('script'):
        text = script.string or ""
        match = re.search(r'https?://[^\s\'"]+/out\?[^\s\'"]+', text)
        if match:
            continue_link = match.group(0)
            break

print(f"Continue link: {continue_link}")

if continue_link:
    print("\nSTEP 2: Following 'Continue to Destination' link -> cyberloom.best/out")
    print("-" * 60)
    # No need to wait since we're programmatic
    resp2 = scraper.get(continue_link, allow_redirects=True, timeout=15)
    out_url = resp2.url
    print(f"Got: {out_url[:80]}")
    print(f"Status: {resp2.status_code}")
    
    # Check if we landed on messycloud
    if 'messycloud' in out_url:
        print(f"\n*** SUCCESS! Final messycloud link: {out_url} ***")
    else:
        print(f"\nNot messycloud yet. Analyzing page...")
        soup2 = BeautifulSoup(resp2.text, 'html.parser')
        
        # Look for any links
        all_links = soup2.find_all('a', href=True)
        print(f"Links on page ({len(all_links)}):")
        for a in all_links[:10]:
            href = a.get('href', '')
            text = a.get_text(strip=True)
            if href and href != '#':
                print(f"  {text[:30]:30s} -> {href[:80]}")
                if 'messycloud' in href:
                    print(f"\n*** FOUND MESSYCLOUD: {href} ***")
        
        # Check for meta refresh or JS redirect
        meta_refresh = soup2.find('meta', attrs={'http-equiv': 'refresh'})
        if meta_refresh:
            content = meta_refresh.get('content', '')
            print(f"\nMeta refresh found: {content}")
            url_match = re.search(r'url=(.*)', content, re.I)
            if url_match:
                next_url = url_match.group(1)
                print(f"Following meta refresh to: {next_url}")
                resp3 = scraper.get(next_url, allow_redirects=True, timeout=15)
                print(f"Final URL: {resp3.url}")
        
        # Check scripts for redirect
        for script in soup2.find_all('script'):
            text = script.string or ""
            if 'messycloud' in text:
                messy_match = re.search(r'https?://[^\s\'"]+messycloud[^\s\'"]+', text)
                if messy_match:
                    print(f"\n*** FOUND MESSYCLOUD IN SCRIPT: {messy_match.group(0)} ***")
            
            # Look for window.location redirects
            loc_match = re.findall(r'(?:window\.location|location\.href)\s*[=]\s*[\'"]([^\'"]+)[\'"]', text)
            for loc in loc_match:
                print(f"  JS redirect to: {loc[:80]}")
                if 'messycloud' in loc:
                    print(f"\n*** FOUND MESSYCLOUD: {loc} ***")
        
        # If we're still on cyberloom/out, try following allow_redirects=False
        print("\n\nSTEP 3: Trying to follow redirects more carefully...")
        print("-" * 60)
        resp3 = scraper.get(continue_link, allow_redirects=False, timeout=15)
        print(f"Status without redirects: {resp3.status_code}")
        if resp3.status_code in (301, 302, 303, 307, 308):
            location = resp3.headers.get('Location', '')
            print(f"Location header: {location[:80]}")
            if location:
                resp4 = scraper.get(location, allow_redirects=True, timeout=15)
                print(f"Final after following Location: {resp4.url[:80]}")
                if 'messycloud' in resp4.url:
                    print(f"\n*** SUCCESS! MESSYCLOUD: {resp4.url} ***")
else:
    print("Could not find continue link!")
