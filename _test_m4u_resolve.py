import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup
import re

def resolve_mdrive_link(md_url: str) -> str:
    """Follow mdrive.ink/mdisk links to get final destination."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    
    try:
        # Step 1: Get the mdrive.ink page
        print(f"Step 1: Fetching {md_url}")
        r1 = session.get(md_url, verify=False, timeout=15, allow_redirects=True)
        print(f"  Status: {r1.status_code}")
        print(f"  URL after redirects: {r1.url}")
        
        # Check if it's already a direct link
        if 'drive.google.com' in r1.url or 'mega.nz' in r1.url:
            return r1.url
            
        soup = BeautifulSoup(r1.text, 'html.parser')
        
        # Look for redirect URLs in the page
        # Common patterns: window.location, meta refresh, or redirect buttons
        
        # Check for JavaScript redirect
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string:
                # Look for window.location patterns
                match = re.search(r'window\.location\.href\s*=\s*["\']([^"\']+)["\']', script.string)
                if match:
                    redirect_url = match.group(1)
                    print(f"  Found JS redirect: {redirect_url}")
                    if redirect_url.startswith('/'):
                        from urllib.parse import urljoin
                        redirect_url = urljoin(r1.url, redirect_url)
                    return redirect_url
                    
                # Look for location.replace
                match = re.search(r'window\.location\.replace\(["\']([^"\']+)["\']\)', script.string)
                if match:
                    redirect_url = match.group(1)
                    print(f"  Found JS replace: {redirect_url}")
                    if redirect_url.startswith('/'):
                        from urllib.parse import urljoin
                        redirect_url = urljoin(r1.url, redirect_url)
                    return redirect_url
        
        # Check for meta refresh
        meta_refresh = soup.find('meta', attrs={'http-equiv': 'refresh'})
        if meta_refresh:
            content = meta_refresh.get('content', '')
            match = re.search(r'url=([^;]+)', content)
            if match:
                redirect_url = match.group(1)
                print(f"  Found meta refresh: {redirect_url}")
                return redirect_url
        
        # Check for download/redirect buttons
        buttons = soup.find_all('a', href=True)
        for btn in buttons:
            text = btn.get_text(strip=True).lower()
            href = btn['href']
            if any(x in text for x in ['download', 'get link', 'go', 'continue', 'redirect']):
                print(f"  Found button: {text} -> {href}")
                if href.startswith('/'):
                    from urllib.parse import urljoin
                    href = urljoin(r1.url, href)
                return href
        
        # If no redirect found, return the final URL
        return r1.url
        
    except Exception as e:
        print(f"  Error: {e}")
        return md_url

# Test with a real movie from Movies4U
print("="*60)
print("Testing Movies4U scraping with final link resolution")
print("="*60)

# First get a movie page
from movie_service import m4u_search
results = m4u_search("avatar", 1)
if results:
    print(f"\nFound movie: {results[0]['title']}")
    print(f"URL: {results[0]['url']}")
    
    # Get the movie details
    from movie_service import m4u_movie_links
    data = m4u_movie_links(results[0]['url'])
    
    print(f"\nFound {len(data.get('episodes', []))} episodes/qualities")
    
    # Try to resolve the first link
    if data.get('episodes'):
        ep = data['episodes'][0]
        qualities = ep.get('qualities', {})
        
        for quality, links in list(qualities.items())[:1]:
            print(f"\n{quality}:")
            for lk in links[:2]:  # Test first 2 links
                url = lk.get('url', '')
                label = lk.get('label', '')
                print(f"  Original: {url[:80]}...")
                print(f"  Label: {label}")
                
                if 'mdrive.ink' in url or 'mdisk' in url:
                    print("  Resolving...")
                    final = resolve_mdrive_link(url)
                    print(f"  Final: {final[:100]}...")
                print()
else:
    print("No movies found")
