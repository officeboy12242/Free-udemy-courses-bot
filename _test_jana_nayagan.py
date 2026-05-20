import sys
sys.stdout.reconfigure(encoding='utf-8')
from movie_service import m4u_search, m4u_movie_links, format_m4u_message

print("="*70)
print("Testing Movies4U with 'Jana Nayagan'")
print("="*70)

# Search for the movie
print("\n1. SEARCHING...")
results = m4u_search('jana nayagan', 5)
print(f"   Found {len(results)} result(s)\n")

if not results:
    print("❌ No results found!")
    sys.exit(0)

# Show all results
for i, r in enumerate(results):
    print(f"   [{i+1}] {r['title']}")
    print(f"       URL: {r['url']}")
    print()

# Get the first result
movie = results[0]
print("="*70)
print(f"2. SCRAPING: {movie['title']}")
print("="*70)

# Get movie links
data = m4u_movie_links(movie['url'])

print(f"\n   Title: {data.get('title', movie['title'])}")
print(f"   Poster: {data.get('poster', 'None')}")
print(f"   Info: {data.get('info', {})}")
print(f"   Total Links Found: {len(data.get('links', []))}")

print("\n" + "="*70)
print("3. ALL EXTRACTED LINKS (RAW):")
print("="*70)

for i, lk in enumerate(data.get('links', []), 1):
    label = lk.get('label', 'N/A')
    name = lk.get('name', 'N/A')
    url = lk.get('url', 'N/A')
    print(f"\n   [{i}] Label: {label}")
    print(f"       Name: {name}")
    print(f"       URL: {url}")

print("\n" + "="*70)
print("4. FORMATTED TELEGRAM MESSAGE:")
print("="*70)
msg = format_m4u_message(movie['title'], data)
print(msg)
print("\n" + "="*70)
print("DONE!")
print("="*70)
