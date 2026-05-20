import sys
sys.stdout.reconfigure(encoding='utf-8')
import requests
from bs4 import BeautifulSoup
import re
from urllib.parse import urljoin

def resolve_mdrive(url: str, depth: int = 0, max_depth: int = 5) -> str:
    """Resolve mdrive.ink/mdisk links to final destination."""
    if depth > max_depth:
        return url
        
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    
    try:
        print(f"{'  '*depth}Resolving: {url}")
        resp = session.get(url, verify=False, timeout=20, allow_redirects=True)
        final_url = resp.url
        print(f"{'  '*depth}After redirects: {final_url}")
        
        # Check if we've reached a final destination
        if any(x in final_url for x in ['drive.google.com', 'mega.nz', 'pixeldrain.com', 'gofile.io', '1fichier.com', 'anonfiles', 'bayfiles']):
            print(f"{'  '*depth}✓ Final destination reached!")
            return final_url
            
        # Check if it's a streaming/video link
        if any(x in final_url for x in ['video-gen.xyz', 'cdn.video-gen.xyz', 'instant.video-gen.xyz']):
            print(f"{'  '*depth}✓ Video link reached!")
            return final_url
            
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Look for redirect buttons or links
        # Common patterns on intermediate pages
        
        # 1. Look for "Get Link", "Download", "Continue" buttons
        for a in soup.find_all('a', href=True):
            text = a.get_text(strip=True).lower()
            href = a['href']
            
            if any(x in text for x in ['get link', 'download', 'continue', 'go to download', 'generate link']):
                if href.startswith('/'):
                    href = urljoin(final_url, href)
                print(f"{'  '*depth}Found button: {text} -> {href}")
                return resolve_mdrive(href, depth + 1, max_depth)
        
        # 2. Look for JavaScript redirects
        scripts = soup.find_all('script')
        for script in scripts:
            if script.string:
                # window.location patterns
                patterns = [
                    r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
                    r'window\.location\.replace\(["\']([^"\']+)["\']\)',
                    r'location\.href\s*=\s*["\']([^"\']+)["\']',
                ]
                for pattern in patterns:
                    match = re.search(pattern, script.string)
                    if match:
                        redirect_url = match.group(1)
                        if redirect_url.startswith('/'):
                            redirect_url = urljoin(final_url, redirect_url)
                        print(f"{'  '*depth}Found JS redirect: {redirect_url}")
                        return resolve_mdrive(redirect_url, depth + 1, max_depth)
        
        # 3. Look for meta refresh
        meta = soup.find('meta', attrs={'http-equiv': 'refresh'})
        if meta:
            content = meta.get('content', '')
            match = re.search(r'url=([^;]+)', content, re.IGNORECASE)
            if match:
                redirect_url = match.group(1)
                if redirect_url.startswith('/'):
                    redirect_url = urljoin(final_url, redirect_url)
                print(f"{'  '*depth}Found meta refresh: {redirect_url}")
                return resolve_mdrive(redirect_url, depth + 1, max_depth)
        
        # 4. Look for iframe (sometimes used for embeds)
        iframe = soup.find('iframe', src=True)
        if iframe:
            src = iframe['src']
            print(f"{'  '*depth}Found iframe: {src}")
            if src.startswith('/'):
                src = urljoin(final_url, src)
            return resolve_mdrive(src, depth + 1, max_depth)
        
        # 5. Look for form submission (sometimes used)
        form = soup.find('form', action=True)
        if form:
            action = form['action']
            if action.startswith('/'):
                action = urljoin(final_url, action)
            print(f"{'  '*depth}Found form action: {action}")
            # Try POST request
            try:
                inputs = {inp.get('name'): inp.get('value') for inp in form.find_all('input')}
                resp2 = session.post(action, data=inputs, verify=False, timeout=20)
                return resolve_mdrive(resp2.url, depth + 1, max_depth)
            except:
                pass
        
        return final_url
        
    except Exception as e:
        print(f"{'  '*depth}Error: {e}")
        return url

# Test with real mdrive links
print("="*60)
print("Testing mdrive.ink link resolution")
print("="*60)

test_urls = [
    "https://mdrive.ink/mdisk/88785",
    "https://mdrive.ink/mdisk/88787",
]

for url in test_urls:
    print(f"\n{'='*60}")
    print(f"Testing: {url}")
    print('='*60)
    final = resolve_mdrive(url)
    print(f"\nFinal URL: {final}")
    print()
