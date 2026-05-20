import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import m4u_search, m4u_movie_links, format_m4u_message

print("Testing Movies4U formatting...")
r = m4u_search('avatar', 1)
if r:
    print(f"Found: {r[0]['title']}\n")
    data = m4u_movie_links(r[0]['url'])
    msg = format_m4u_message(r[0]['title'], data)
    print(msg)
else:
    print("No results")
