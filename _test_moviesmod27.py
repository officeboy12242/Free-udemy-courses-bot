import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
import re
from bs4 import BeautifulSoup

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# 1. Fetch modpro.blog
r1 = requests.get('https://episodes.modpro.blog/archives/9131', headers=headers, verify=False)
soup1 = BeautifulSoup(r1.text, 'html.parser')
sid_url = None
for a in soup1.find_all('a', href=True):
    if 'Episode 1' in a.get_text(strip=True):
        sid_url = a['href']
        break

# 2. Fetch sid_url (GET)
r2 = requests.get(sid_url, headers=headers, verify=False)
soup2 = BeautifulSoup(r2.text, 'html.parser')
form1 = soup2.find('form', id='landing')
action1 = form1.get('action')
data1 = {inp.get('name'): inp.get('value') for inp in form1.find_all('input')}

# 3. POST form1
r3 = requests.post(action1, data=data1, headers=headers, verify=False)
soup3 = BeautifulSoup(r3.text, 'html.parser')
form2 = soup3.find('form', id='landing')
action2 = form2.get('action')
data2 = {inp.get('name'): inp.get('value') for inp in form2.find_all('input')}

# 4. POST form2
r4 = requests.post(action2, data=data2, headers=headers, verify=False)

# 5. Extract ?go= link
m = re.search(r'setAttribute\("href","([^"]+)"\)', r4.text)
if m:
    go_url = m.group(1)
    
    # 6. Fetch go_url
    r5 = requests.get(go_url, headers=headers, verify=False)
    print(r5.text[:1000])
