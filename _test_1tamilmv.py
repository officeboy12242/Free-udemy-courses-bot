"""Test script for 1TamilMV scraping."""
import os
import sys
import re
import time
import urllib3
from dotenv import load_dotenv

# Suppress SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

load_dotenv()

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests
import cloudscraper

try:
    from curl_cffi import requests as cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    cffi_requests = None
    _CFFI_AVAILABLE = False

from bs4 import BeautifulSoup

# Create a cloudscraper session to bypass Cloudflare
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'mobile': False
    }
)

TAMILMV_BASE = "https://www.1tamilmv.futbol"

def _get(url, **kwargs):
    """Get URL with cloudscraper to bypass Cloudflare."""
    # Use cloudscraper which handles Cloudflare automatically
    time.sleep(1)  # Small delay to avoid rate limiting
    return scraper.get(url, **kwargs)

def tamilmv_latest_movies(limit=10):
    """Scrape recently added movies from homepage."""
    try:
        resp = _get(TAMILMV_BASE + "/", timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        movies = []
        
        # Find "RECENTLY ADDED" section
        recently_added = None
        for h3 in soup.find_all(['h3', 'h2', 'strong']):
            if 'RECENTLY ADDED' in h3.get_text().upper():
                recently_added = h3
                break
        
        if recently_added:
            # Get the parent container and find all movie links after it
            parent = recently_added.find_parent()
            for link in parent.find_all('a', href=True):
                if len(movies) >= limit:
                    break
                
                href = link.get('href', '')
                text = link.get_text(strip=True)
                
                # Skip empty or navigation links
                if not text or len(text) < 10:
                    continue
                
                # Look for typical movie title patterns
                if any(x in text for x in ['1080p', '720p', '480p', 'WEB-DL', 'BluRay', 'HD']):
                    url = href if href.startswith('http') else TAMILMV_BASE + href
                    movies.append({
                        'title': text[:100],  # Truncate long titles
                        'url': url,
                        'poster': '',
                        'source': 'tamilmv'
                    })
        
        print(f"[OK] Found {len(movies)} recently added movies")
        return movies
    except Exception as e:
        print(f"[ERROR] Error scraping latest: {e}")
        return []

def tamilmv_search(query, limit=10):
    """Search 1TamilMV."""
    try:
        # 1TamilMV uses a standard forum search
        search_url = f"{TAMILMV_BASE}/index.php?/search/&q={query}&type=forums_topic&search_and_or=and&search_in=titles"
        resp = _get(search_url, timeout=20)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        movies = []
        
        # Find search results - they're in article tags or list items
        for item in soup.select('article, li.ipsStreamItem, .cSearchResult'):
            if len(movies) >= limit:
                break
            
            # Find title link
            title_link = item.select_one('a.ipsType_break, h2 a, .ipsDataItem_title a')
            if not title_link:
                continue
            
            title = title_link.get_text(strip=True)
            href = title_link.get('href', '')
            
            if not title or not href:
                continue
            
            url = href if href.startswith('http') else TAMILMV_BASE + href
            
            movies.append({
                'title': title[:100],
                'url': url,
                'poster': '',
                'source': 'tamilmv'
            })
        
        print(f"[OK] Found {len(movies)} search results for '{query}'")
        return movies
    except Exception as e:
        print(f"[ERROR] Search failed: {e}")
        return []

def tamilmv_movie_links(movie_url):
    """Extract download links from a movie page."""
    try:
        resp = _get(movie_url, timeout=25)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        
        links = []
        
        # Find the main content/post
        content = soup.select_one('div[data-role="commentContent"], .ipsType_richText, .cPost_contentWrap, article')
        
        if not content:
            print("[WARNING] Could not find content area")
            # Try to find any article or post content
            content = soup.select_one('article, .post, [class*="content"]')
            if not content:
                print("[ERROR] Really could not find any content")
                return {'poster': '', 'info': {}, 'links': [], 'episodes': [], 'is_series': False}
        
        print(f"[DEBUG] Found content area, looking for links...")
        
        # Look for spoiler/hidden content first
        spoilers = content.find_all(['details', 'div'], class_=re.compile(r'spoiler|hidden|ipsToggle', re.I))
        print(f"[DEBUG] Found {len(spoilers)} spoiler/hidden sections")
        
        # Look for download links
        # 1TamilMV typically uses text like "Download Link" followed by actual link
        all_links = content.find_all('a', href=True)
        print(f"[DEBUG] Found {len(all_links)} total links in content")
        
        for i, a in enumerate(all_links):
            href = a.get('href', '')
            text = a.get_text(strip=True)
            
            # Debug: print all links
            print(f"[DEBUG {i+1}/{len(all_links)}] {text[:40]} -> {href[:80]}")
            
            # Skip magnet links
            if href.startswith('magnet:'):
                print(f"[SKIP] Magnet link")
                continue
            
            # Skip torrent files
            if '.torrent' in href.lower():
                print(f"[SKIP] Torrent file")
                continue
            
            # Check if this is a "DIRECT LINK" button (1TamilMV pattern)
            if text == "DIRECT LINK" or "direct" in text.lower():
                print(f"[FOUND] Direct link button: {href[:60]}...")
                
                # Try to follow redirects to get the FINAL messycloud.ink URL
                try:
                    final_url = href
                    
                    # Follow the full redirect chain: cyberloom/l -> inkvoyage/stackmint -> cyberloom/out -> messycloud
                    if 'cyberloom' in href or '/l/' in href:
                        print(f"[STEP 1] Following cyberloom redirect...")
                        redirect_resp = scraper.get(href, allow_redirects=True, timeout=15)
                        intermediate_url = redirect_resp.url
                        print(f"[STEP 1] Got: {intermediate_url[:60]}...")
                        
                        # Parse page to find "Continue to Destination" link
                        page_soup = BeautifulSoup(redirect_resp.text, 'html.parser')
                        continue_link = None
                        for page_link in page_soup.find_all('a', href=True):
                            if '/out?' in page_link.get('href', '') or 'continue' in page_link.get_text(strip=True).lower():
                                continue_link = page_link.get('href')
                                break
                        
                        if continue_link:
                            print(f"[STEP 2] Following 'Continue' link...")
                            time.sleep(1)
                            final_resp = scraper.get(continue_link, allow_redirects=True, timeout=15)
                            final_url = final_resp.url
                            print(f"[STEP 2] Final URL: {final_url[:60]}...")
                        else:
                            print(f"[WARNING] No continue link found")
                            final_url = intermediate_url
                    
                    # Extract quality from surrounding text
                    # Walk backward through siblings to find the quality text
                    parent_text = ""
                    prev = a.find_previous(['strong', 'b', 'p'])
                    if prev:
                        parent_text = prev.get_text()
                    
                    quality = 'Unknown'
                    if '1080p' in parent_text:
                        quality = '1080p'
                    elif '720p' in parent_text:
                        quality = '720p'
                    elif '480p' in parent_text or 'rip' in parent_text.lower() or '500MB' in parent_text:
                        quality = '480p/Rip'
                    elif '4K' in parent_text or '2160p' in parent_text:
                        quality = '4K'
                    
                    links.append({
                        'label': f'{quality} - Direct Download',
                        'url': final_url
                    })
                    print(f"[OK] Added link ({quality}): {final_url[:60]}...")
                    
                except Exception as redirect_err:
                    print(f"[WARNING] Could not follow redirect: {redirect_err}")
                    # Add the shortener link anyway
                    links.append({
                        'label': text or 'Download',
                        'url': href
                    })
            
            # Also check for direct cloud storage links (backup method)
            cloud_domains = ['messycloud.ink', 'mixdrop', 'streamtape', 'dood', 
                            'filemoon', 'streamwish', 'vtube', 'upstream']
            
            if any(domain in href.lower() for domain in cloud_domains):
                # Extract quality/version from surrounding text
                parent_text = a.find_parent().get_text() if a.find_parent() else text
                
                quality = 'Unknown'
                if '1080p' in parent_text:
                    quality = '1080p'
                elif '720p' in parent_text:
                    quality = '720p'
                elif '480p' in parent_text:
                    quality = '480p'
                elif '4K' in parent_text or '2160p' in parent_text:
                    quality = '4K'
                
                links.append({
                    'label': f'{quality} - {text or "Download"}',
                    'url': href
                })
                print(f"[OK] Found cloud link: {quality} - {href[:60]}...")
        
        # Also look for links in <strong> or <b> tags that might contain download sections
        for strong in content.find_all(['strong', 'b']):
            if any(x in strong.get_text().upper() for x in ['DOWNLOAD', 'LINK', 'WATCH']):
                # Get next sibling links
                next_elem = strong.find_next_sibling()
                if next_elem:
                    for a in next_elem.find_all('a', href=True) if hasattr(next_elem, 'find_all') else []:
                        href = a.get('href', '')
                        
                        if href.startswith('magnet:') or '.torrent' in href.lower():
                            continue
                        
                        cloud_domains = ['messycloud.ink', 'mixdrop', 'streamtape', 'dood', 
                                        'filemoon', 'streamwish', 'vtube', 'upstream']
                        
                        if any(domain in href.lower() for domain in cloud_domains):
                            links.append({
                                'label': a.get_text(strip=True) or 'Download',
                                'url': href
                            })
                            print(f"[OK] Found cloud link: {href[:60]}...")
        
        print(f"\n[TOTAL] Direct download links found: {len(links)}")
        
        return {
            'poster': '',
            'info': {},
            'links': links,
            'episodes': [],
            'is_series': False
        }
    except Exception as e:
        print(f"[ERROR] Error extracting links: {e}")
        return {'poster': '', 'info': {}, 'links': [], 'episodes': [], 'is_series': False}

if __name__ == "__main__":
    print("=" * 80)
    print("Testing 1TamilMV Scraper")
    print("=" * 80)
    
    # First, visit homepage to get cookies
    print("\n[INIT] Visiting homepage to establish session...")
    try:
        _get(TAMILMV_BASE + "/", timeout=15)
        print("[OK] Session established")
    except Exception as e:
        print(f"[WARNING] Could not visit homepage: {e}")
    
    time.sleep(2)
    
    # Test 1: Get recently added movies
    print("\n" + "=" * 80)
    print("TEST 1: Recently Added (First 10)")
    print("=" * 80)
    recent = tamilmv_latest_movies(limit=10)
    for i, movie in enumerate(recent, 1):
        print(f"\n{i}. {movie['title']}")
        print(f"   URL: {movie['url']}")
    
    # Test 2: Search for "walk"
    print("\n\n" + "=" * 80)
    print("TEST 2: Search for 'walk'")
    print("=" * 80)
    results = tamilmv_search("walk", limit=5)
    for i, movie in enumerate(results, 1):
        print(f"\n{i}. {movie['title']}")
        print(f"   URL: {movie['url']}")
    
    # Test 3: Get download links for The Walk (2015) (result #2)
    if len(results) >= 2:
        print("\n\n" + "=" * 80)
        print(f"TEST 3: Getting download links for: {results[1]['title']}")
        print("=" * 80)
        links_data = tamilmv_movie_links(results[1]['url'])
        
        print(f"\n[FINAL OUTPUT] - Direct Download Links:")
        print("=" * 80)
        if links_data['links']:
            for i, link in enumerate(links_data['links'], 1):
                print(f"\n{i}. {link['label']}")
                print(f"   LINK: {link['url']}")
        else:
            print("[ERROR] No direct download links found (only magnets/torrents)")
