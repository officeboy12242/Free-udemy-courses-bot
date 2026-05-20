import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

url = 'https://cloud.unblockedgames.world/t-mobile-is-providing-microsoft-surface-pro-9-users-with-30gb-of-complimentary-5g-data/'
data = {
    "_wp_http2": "eJwFwUtygyAAANArKcTusghFdGzAgnwiO8WMVLFxRtMQT9/3OtieumOGjbSZ0KvvU5cMyTrfcQj9FJ46YY3E4sZMqJulshITpFLxovDyknnV9KXf7pO9uoW1/CCbLjTgc6RqWbdBCSug5yx/ZA6MQJX+QY1AbNnBYPhhTYR2UukweykheTdEwL4M2t4842GEMgy7SPz8BS8nB9LJEI07qREPorqbNKpfvVpgxYAFdon46PMk2iK07DYCo7K9w+TPzjxrboheQQDUED6E8a0CQvIIdZuQD1OwvJY0kwuFEvvYTlrbJZS0SEtHdGaP8N2A+Gmkp+xnX5QhR4eRNBg96XuLrIitUxVluUV17g61uHR4nc//g6xy2g==",
    "token": "eFUrWGc1c1NJaWs5WHlVSmhBcllDdz09"
}
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}
r = requests.post(url, data=data, headers=headers, verify=False)
soup = BeautifulSoup(r.text, 'html.parser')

print(f"Elements with 'verify':")
for el in soup.find_all(id=lambda x: x and 'verify' in x.lower()):
    print(f"  {el.name} id={el.get('id')} class={el.get('class')} text={el.get_text(strip=True)[:50]}")

print(f"Elements with 'step':")
for el in soup.find_all(id=lambda x: x and 'step' in x.lower()):
    print(f"  {el.name} id={el.get('id')} class={el.get('class')} text={el.get_text(strip=True)[:50]}")
