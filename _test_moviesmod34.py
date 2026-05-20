import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

session = requests.Session()
session.headers.update(headers)

# 1. Fetch modpro.blog
r1 = session.get('https://episodes.modpro.blog/archives/9131', verify=False)
soup1 = BeautifulSoup(r1.text, 'html.parser')
sid_url = None
for a in soup1.find_all('a', href=True):
    if 'Episode 1' in a.get_text(strip=True):
        sid_url = a['href']
        break

# 2. Fetch sid_url (GET)
r2 = session.get(sid_url, verify=False)
soup2 = BeautifulSoup(r2.text, 'html.parser')
form1 = soup2.find('form', id='landing')
action1 = form1.get('action')
data1 = {inp.get('name'): inp.get('value') for inp in form1.find_all('input')}

# 3. POST form1
r3 = session.post(action1, data=data1, verify=False)
soup3 = BeautifulSoup(r3.text, 'html.parser')

print("Timer elements:")
for el in soup3.find_all(id='timer'):
    print(el)
