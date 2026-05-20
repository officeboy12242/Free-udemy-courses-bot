import sys, os
sys.stdout.reconfigure(encoding='utf-8')

import requests
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

print(f"SID URL: {sid_url}")

# 2. Fetch sid_url (GET)
r2 = requests.get(sid_url, headers=headers, verify=False)
soup2 = BeautifulSoup(r2.text, 'html.parser')
form1 = soup2.find('form', id='landing')
if not form1:
    print("No landing form found!")
    sys.exit(1)

action1 = form1.get('action')
data1 = {}
for inp in form1.find_all('input'):
    data1[inp.get('name')] = inp.get('value')

print(f"Form 1 Action: {action1}")
print(f"Form 1 Data: {data1}")

# 3. POST form1
r3 = requests.post(action1, data=data1, headers=headers, verify=False)
soup3 = BeautifulSoup(r3.text, 'html.parser')
form2 = soup3.find('form', id='landing')
if not form2:
    print("No second landing form found!")
    sys.exit(1)

action2 = form2.get('action')
data2 = {}
for inp in form2.find_all('input'):
    data2[inp.get('name')] = inp.get('value')

print(f"Form 2 Action: {action2}")
print(f"Form 2 Data: {data2}")

# 4. POST form2
r4 = requests.post(action2, data=data2, headers=headers, verify=False)
soup4 = BeautifulSoup(r4.text, 'html.parser')

print("Elements with 'verify':")
for el in soup4.find_all(id=lambda x: x and 'verify' in x.lower()):
    print(f"  {el.name} id={el.get('id')} class={el.get('class')} text={el.get_text(strip=True)[:50]}")

print("Elements with 'step':")
for el in soup4.find_all(id=lambda x: x and 'step' in x.lower()):
    print(f"  {el.name} id={el.get('id')} class={el.get('class')} text={el.get_text(strip=True)[:50]}")
