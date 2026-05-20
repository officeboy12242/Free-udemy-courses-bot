import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

print("Testing fixes for MoviesMod and gadgetsweb resolution")
print("="*60)

# Test 1: Check bot_with_healthcheck has moviesmod case
print("\n1. Checking bot_with_healthcheck.py for moviesmod case...")
with open('bot_with_healthcheck.py', 'r', encoding='utf-8') as f:
    content = f.read()
    if 'elif source == "moviesmod":' in content:
        print("   ✓ moviesmod case found in _post_movie_to_channel")
    else:
        print("   ✗ ERROR: moviesmod case NOT found in _post_movie_to_channel!")

    if 'format_moviesmod_message' in content:
        print("   ✓ format_moviesmod_message is imported/used")
    else:
        print("   ✗ ERROR: format_moviesmod_message not found!")

# Test 2: Test MoviesMod with a working URL
print("\n2. Testing MoviesMod with latest movie...")
from movie_service import moviesmod_latest_movies, moviesmod_movie_links, format_moviesmod_message

try:
    movies = moviesmod_latest_movies(page=1)
    if movies:
        first = movies[0]
        print(f"   First movie: {first['title'][:50]}...")
        print(f"   URL: {first['url'][:60]}...")

        detail = moviesmod_movie_links(first['url'])
        print(f"   Links found: {len(detail.get('links', []))}")

        if detail.get('links'):
            for i, lk in enumerate(detail['links'][:3]):
                url_preview = lk.get('url', '')[:60]
                print(f"   Link {i+1}: {lk.get('label', 'N/A')[:40]}...")
                print(f"      URL: {url_preview}...")

            # Format message
            formatted = format_moviesmod_message(first['title'], detail)
            print(f"   Formatted length: {len(formatted)} chars")

            if "No download" in formatted:
                print("   ✗ ERROR: Message shows no download links!")
            else:
                print("   ✓ Message has download links")
        else:
            print("   ⚠ No links found for this movie")
    else:
        print("   ✗ No movies returned from latest listing")
except Exception as e:
    print(f"   ✗ ERROR: {type(e).__name__}: {str(e)[:100]}")

print("\n" + "="*60)
print("Done!")
