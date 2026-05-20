import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import m4u_search, m4u_movie_links, format_m4u_message

print("Searching for 'avatar' on Movies4U...")
results = m4u_search('avatar', 3)
print(f"Found {len(results)} results\n")

for i, r in enumerate(results):
    print(f"{i+1}. {r['title']}")
print()

if results:
    # Get first result
    movie = results[0]
    print(f"\nGetting links for: {movie['title']}")
    print("="*60)
    
    data = m4u_movie_links(movie['url'])
    print(f"\nPoster: {data.get('poster', 'None')}")
    print(f"Info: {data.get('info', {})}")
    print(f"\nNumber of links: {len(data.get('links', []))}")
    
    print("\n--- Raw Links ---")
    for lk in data.get('links', []):
        print(f"Label: {lk.get('label')}")
        print(f"Name: {lk.get('name')}")
        print(f"URL: {lk.get('url')}")
        print()
    
    print("\n--- Formatted Message ---")
    msg = format_m4u_message(movie['title'], data)
    print(msg)
