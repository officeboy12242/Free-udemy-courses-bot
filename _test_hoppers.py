import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import m4u_search, m4u_movie_links, format_m4u_message

print("="*70)
print("Testing Movies4U Formatting - Hoppers")
print("="*70)

results = m4u_search('hoppers', 3)
print(f"\nFound {len(results)} result(s)\n")

for i, r in enumerate(results, 1):
    print(f"[{i}] {r['title']}")
    print(f"    URL: {r['url']}")
print()

if results:
    movie = results[0]
    print("="*70)
    print(f"SCRAPING: {movie['title']}")
    print("="*70)
    
    data = m4u_movie_links(movie['url'])
    
    print(f"\nTotal Links: {len(data.get('links', []))}")
    print("\n" + "-"*70)
    print("FORMATTED OUTPUT:")
    print("-"*70 + "\n")
    
    msg = format_m4u_message(movie['title'], data)
    print(msg)
else:
    print("No results found for 'hoppers'")
