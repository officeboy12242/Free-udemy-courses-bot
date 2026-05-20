import sys
sys.stdout.reconfigure(encoding='utf-8')

# Test all latest movies functions
from movie_service import (
    hdhub_latest_movies, hdh_latest_movies, md_latest_movies,
    m4u_latest_movies, vega_latest_movies, sdmp_latest_movies,
    bollyflix_latest_movies, moviesmod_latest_movies
)

print("Testing /movies command - all sources\n")
print("="*60)

test_functions = [
    ("HDHub4u", hdhub_latest_movies),
    ("4KHDHub", hdh_latest_movies),
    ("MoviesDrive", md_latest_movies),
    ("Movies4U", m4u_latest_movies),
    ("Vegamovies", vega_latest_movies),
    ("SDMoviesPoint", sdmp_latest_movies),
    ("BollyFlix", bollyflix_latest_movies),
    ("MoviesMod", moviesmod_latest_movies),
]

for name, func in test_functions:
    print(f"\nTesting {name}...")
    try:
        results = func(page=1)
        if results:
            print(f"  ✓ SUCCESS - Found {len(results)} movies")
            print(f"    First: {results[0]['title'][:50]}...")
        else:
            print(f"  ✗ EMPTY - No movies returned")
    except Exception as e:
        print(f"  ✗ ERROR - {type(e).__name__}: {str(e)[:100]}")

print("\n" + "="*60)
print("Done!")
