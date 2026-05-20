import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

url = 'https://cdn.video-gen.xyz/84c2b9b5c23553459664f772205f4073a9da9fb4461419fbc208267a4c12d38402df4e09f198005f8408e8390680784b820bf5edc8cd986f9198477e951f47d364c51224fe2cb33ce4e088710db410ad1ca1b077afd3b9297e90441f9409d8e56a4486c97a27d316e4d036aa227abf8e::f4208f9dfbc8f1046d88f1516decca56'
r = requests.head(url, headers=headers, verify=False, allow_redirects=True)
print(f"Status: {r.status_code}")
print(f"Final URL: {r.url}")
