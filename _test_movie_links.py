import sys, asyncio
sys.stdout.reconfigure(encoding='utf-8')

from movie_service import (
    hdhub_movie_links, hdh_movie_links, md_movie_links,
    m4u_movie_links, vega_movie_links, sdmp_movie_links,
    bollyflix_movie_links, moviesmod_movie_links,
    hdhub_latest_movies, hdh_latest_movies, md_latest_movies,
    m4u_latest_movies, vega_latest_movies, sdmp_latest_movies,
    bollyflix_latest_movies, moviesmod_latest_movies
)

print("Testing /movies workflow - list then get links\n")
print("="*60)

# Test each source: get first movie, then try to get links
test_sources = [
    ("HDHub4u", hdhub_latest_movies, hdhub_movie_links),
    ("4KHDHub", hdh_latest_movies, hdh_movie_links),
    ("MoviesDrive", md_latest_movies, md_movie_links),
    ("Movies4U", m4u_latest_movies, m4u_movie_links),
    ("Vegamovies", vega_latest_movies, vega_movie_links),
    ("SDMoviesPoint", sdmp_latest_movies, sdmp_movie_links),
    ("BollyFlix", bollyflix_latest_movies, bollyflix_movie_links),
    ("MoviesMod", moviesmod_latest_movies, moviesmod_movie_links),
]

for name, list_func, link_func in test_sources:
    print(f"\n{name}:")
    print("-" * 40)

    # Step 1: Get movie list
    try:
        movies = list_func(page=1)
        if not movies:
            print(f"  LIST: ✗ No movies found")
            continue
        print(f"  LIST: ✓ Found {len(movies)} movies")

        # Step 2: Get first movie and try to fetch links
        first_movie = movies[0]
        print(f"  FIRST: {first_movie['title'][:50]}...")

        try:
            detail = link_func(first_movie['url'])
            link_count = len(detail.get('links', []))
            print(f"  LINKS: ✓ Found {link_count} link groups")
            if link_count > 0:
                # Show first link group
                first_link = detail['links'][0]
                print(f"    Sample: {str(first_link)[:80]}...")
        except Exception as e:
            print(f"  LINKS: ✗ ERROR - {type(e).__name__}: {str(e)[:80]}")

    except Exception as e:
        print(f"  LIST: ✗ ERROR - {type(e).__name__}: {str(e)[:80]}")

print("\n" + "="*60)
print("Done!")
