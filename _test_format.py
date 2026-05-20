import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import m4u_search, m4u_movie_links, format_m4u_message

print("="*70)
print("Testing Movies4U Formatting - Jana Nayagan")
print("="*70)

results = m4u_search('jana nayagan', 1)
if results:
    data = m4u_movie_links(results[0]['url'])
    msg = format_m4u_message(results[0]['title'], data)
    print(msg)
else:
    print("No results found")
