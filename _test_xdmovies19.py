import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

url = "https://top.xdmovies.wtf/js/main.js"
r = requests.get(url, impersonate="chrome", timeout=15)
print(f"Status: {r.status_code}")
if r.status_code == 200:
    print(r.text[:1000])
    
    # Let's also search for 'fetch' or 'XMLHttpRequest' in the JS
    import re
    fetches = re.findall(r'fetch\([^\)]+\)', r.text)
    print("\nFetches found:")
    for f in fetches:
        print(f)
