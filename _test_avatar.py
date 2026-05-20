import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import moviesmod_search, moviesmod_movie_links, format_moviesmod_message

print("Searching for 'avatar' on MoviesMod...")
results = moviesmod_search('avatar', 5)
print(f"\nFound {len(results)} results:\n")

for i, r in enumerate(results):
    print(f"{i+1}. {r['title']}")
    print(f"   URL: {r['url']}")
    print()

if results:
    print("\n" + "="*60)
    print(f"Getting links for: {results[0]['title']}")
    print("="*60)
    data = moviesmod_movie_links(results[0]['url'])
    print(f"\nTitle: {data['title']}")
    print(f"Number of links: {len(data['links'])}")
    print("\n--- Links ---")
    for lk in data['links']:
        print(f"\nLabel: {lk['label']}")
        print(f"URL: {lk['url'][:100]}..." if len(lk['url']) > 100 else f"URL: {lk['url']}")
    
    print("\n" + "="*60)
    print("Formatted message:")
    print("="*60)
    msg = format_moviesmod_message(data['title'], data)
    print(msg[:2000])  # Print first 2000 chars
