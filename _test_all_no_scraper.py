import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

os.environ["SCRAPER_API_KEY"] = ""
import movie_service
movie_service.SCRAPER_API_KEY = ""

from movie_service import (
    hdhub_latest_movies, hdh_latest_movies, md_latest_movies,
    m4u_latest_movies, vega_latest_movies, sdmp_latest_movies,
    bollyflix_latest_movies, moviesmod_latest_movies
)

print("Testing ALL sites WITHOUT ScraperAPI (curl_cffi only)")
print("="*60)

sources = [
    ("HDHub4u", hdhub_latest_movies),
    ("4KHDHub", hdh_latest_movies),
    ("MoviesDrive", md_latest_movies),
    ("Movies4U", m4u_latest_movies),
    ("Vegamovies", vega_latest_movies),
    ("SDMoviesPoint", sdmp_latest_movies),
    ("BollyFlix", bollyflix_latest_movies),
    ("MoviesMod", moviesmod_latest_movies),
]

passed = 0
failed = 0
for name, func in sources:
    try:
        movies = func(page=1)
        if movies:
            print(f"  {name:15} -> PASS ({len(movies)} movies)")
            passed += 1
        else:
            print(f"  {name:15} -> FAIL (0 movies)")
            failed += 1
    except Exception as e:
        print(f"  {name:15} -> ERROR: {str(e)[:50]}")
        failed += 1

print(f"\n{'='*60}")
print(f"Results: {passed} PASS / {failed} FAIL")
print(f"{'='*60}")

if failed > 0:
    print("\nFailed sites still need ScraperAPI on Render.")
else:
    print("\nAll sites work without ScraperAPI!")
