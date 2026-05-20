import sys, os
sys.stdout.reconfigure(encoding='utf-8')

with open(r"c:\Users\jaikishanbagul\Downloads\tgbot2\movie_service.py", "a", encoding="utf-8") as f:
    f.write("""

# ══════════════════════════════════════════════════════════════════════════════
#  MoviesMod  (https://moviesmod.farm/)
# ══════════════════════════════════════════════════════════════════════════════

MOVIESMOD_BASE = os.getenv("MOVIESMOD_BASE_URL", "https://moviesmod.farm")

def _resolve_modpro_blog(url: str) -> str:
    \"\"\"
    Resolves episodes.modpro.blog links to their final destination.
    Flow:
    1. GET modpro.blog -> extract sid_url (cloud.unblockedgames.world)
    2. GET sid_url -> extract form1
    3. POST form1 -> extract form2
    4. POST form2 -> extract s_343 cookie and ?go= URL
    5. GET ?go= URL -> extract refresh URL
    6. GET refresh URL -> extract window.location.replace URL
    7. GET replace URL -> extract final download links (video-seed.pro, tgseed.link)
    \"\"\"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    session = requests.Session()
    session.headers.update(headers)
    
    try:
        # 1. Fetch modpro.blog
        r1 = session.get(url, verify=False, timeout=15)
        soup1 = BeautifulSoup(r1.text, 'html.parser')
        sid_url = None
        for a in soup1.find_all('a', href=True):
            if 'Episode 1' in a.get_text(strip=True) or 'All Episodes Batch' in a.get_text(strip=True):
                sid_url = a['href']
                break
                
        if not sid_url:
            return url
            
        # 2. Fetch sid_url (GET)
        r2 = session.get(sid_url, verify=False, timeout=15)
        soup2 = BeautifulSoup(r2.text, 'html.parser')
        form1 = soup2.find('form', id='landing')
        if not form1:
            return sid_url
            
        action1 = form1.get('action')
        data1 = {inp.get('name'): inp.get('value') for inp in form1.find_all('input')}
        
        # 3. POST form1
        r3 = session.post(action1, data=data1, verify=False, timeout=15)
        soup3 = BeautifulSoup(r3.text, 'html.parser')
        form2 = soup3.find('form', id='landing')
        if not form2:
            return action1
            
        action2 = form2.get('action')
        data2 = {inp.get('name'): inp.get('value') for inp in form2.find_all('input')}
        
        # 4. POST form2
        r4 = session.post(action2, data=data2, verify=False, timeout=15)
        
        # Extract cookie
        m_cookie = re.search(r"s_343\\('([^']+)',\\s*'([^']+)'", r4.text)
        if m_cookie:
            c_name = m_cookie.group(1)
            c_value = m_cookie.group(2)
            session.cookies.set(c_name, c_value, domain='cloud.unblockedgames.world')
            
        # 5. Extract ?go= link
        m = re.search(r'setAttribute\\("href","([^"]+)"\\)', r4.text)
        if not m:
            return action2
            
        go_url = m.group(1)
        
        # 6. Fetch go_url
        r5 = session.get(go_url, verify=False, timeout=15)
        
        # 7. Extract refresh URL
        m_refresh = re.search(r'url=([^"]+)', r5.text)
        if not m_refresh:
            return go_url
            
        refresh_url = m_refresh.group(1)
        
        # 8. Fetch refresh_url
        r6 = session.get(refresh_url, verify=False, timeout=15)
        
        # 9. Extract replace URL
        m_replace = re.search(r'window\\.location\\.replace\\("([^"]+)"\\)', r6.text)
        if not m_replace:
            return refresh_url
            
        replace_url = m_replace.group(1)
        if replace_url.startswith('/'):
            from urllib.parse import urljoin
            replace_url = urljoin(r6.url, replace_url)
            
        # 10. Fetch final page
        r7 = session.get(replace_url, verify=False, timeout=15)
        soup7 = BeautifulSoup(r7.text, 'html.parser')
        
        final_links = []
        for a in soup7.find_all('a', href=True):
            text = a.get_text(strip=True).lower()
            href = a['href']
            if 'instant download' in text or 'telegram file' in text or 'direct' in text:
                final_links.append(href)
                
        if final_links:
            return "\\n".join(final_links)
            
        return r7.url
        
    except Exception as e:
        log.error("Error resolving modpro blog %s: %s", url, e)
        return url

def moviesmod_movie_links(movie_url: str) -> dict:
    \"\"\"
    Scrapes moviesmod.farm for download links.
    Returns a dict:
      {
        "title": "Movie Title",
        "links": [
           {"label": "Season 1 [200MB] - Episode Links", "url": "final_url_1\\nfinal_url_2"},
           ...
        ]
      }
    \"\"\"
    try:
        resp = _get(movie_url, timeout=25)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.error("moviesmod_movie_links %s failed: %s", movie_url, exc)
        return {"title": "Error", "links": []}

    # Title
    title_tag = soup.find('h1')
    title = title_tag.get_text(strip=True) if title_tag else "MoviesMod Content"
    
    content = soup.select_one("main, .entry-content, article") or soup
    
    links = []
    current_header = ""
    
    for tag in content.find_all(['h2', 'h3', 'a']):
        if tag.name in ['h2', 'h3']:
            text = tag.get_text(strip=True)
            # Ignore some common non-link headers
            if text.lower() not in ['series info:', 'storyline:', 'screenshots:', 'related posts', 'search movies', 'categories']:
                current_header = text
        elif tag.name == 'a':
            href = tag.get('href', '')
            text = tag.get_text(strip=True)
            
            if 'episodes.modpro.blog' in href:
                label = f"{current_header} - {text}" if current_header else text
                links.append({"label": label, "url": href})
            elif 'uhdmovies.foo' in href:
                label = f"{current_header} - {text}" if current_header else text
                links.append({"label": label, "url": href})
                
    # Now resolve the links in parallel
    resolved_links = []
    
    def _resolve_link(item):
        url = item['url']
        if 'episodes.modpro.blog' in url:
            resolved_url = _resolve_modpro_blog(url)
        else:
            resolved_url = url
        return {"label": item['label'], "url": resolved_url}
        
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(_resolve_link, item) for item in links]
        for future in as_completed(futures, timeout=120):
            try:
                res = future.result()
                if res['url']:
                    resolved_links.append(res)
            except Exception as e:
                log.error("Error resolving moviesmod link: %s", e)
                
    # Sort to maintain some order (maybe by label)
    resolved_links.sort(key=lambda x: x['label'])
    
    return {
        "title": title,
        "links": resolved_links
    }
""")
