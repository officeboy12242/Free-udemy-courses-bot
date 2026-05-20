import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

os.environ["SCRAPER_API_KEY"] = ""
import movie_service
movie_service.SCRAPER_API_KEY = ""

from movie_service import _resolve_gadgetsweb

# Episode 1 gadgetsweb URL from Dutton Ranch
gw_url1 = "https://gadgetsweb.xyz/?id=dnNpaWNMR0RveHpWZGgzRzhwYUdpaHJQMEl2ejZVTU5rb3hJQXV6Q0ZTNUdVUW1VVnlUYmQyc0VGOGZMa0p4dUIwVWFsV25zOTZXNDBFZGhQZEF2cGg3eEttQWtCNkluL253cmtiMEorTGs9"

# Episode 2 gadgetsweb URL from Dutton Ranch
gw_url2 = "https://gadgetsweb.xyz/?id=dnNpaWNMR0RveHpWZGgzRzhwYUdpaHJQMEl2ejZVTU5rb3hJQXV6Q0ZTNUdVUW1VVnlUYmQyc0VGOGZMa0p4dWFTU0FiaG5QcEZhZHEyVlVFTEJQejd2M3dNc2JtdEVWRGJaQVNEK2J2ZVk9"

print("Testing gadgetsweb.xyz direct resolution")
print("="*60)

print(f"\n[EP 1] {gw_url1[:50]}...")
links1 = _resolve_gadgetsweb(gw_url1)
print(f"  Result: {len(links1)} links")
for lk in links1[:5]:
    print(f"    [{lk['label'][:25]}] -> {lk['url'][:55]}")
if len(links1) > 5:
    print(f"    ... +{len(links1)-5} more")

print(f"\n[EP 2] {gw_url2[:50]}...")
links2 = _resolve_gadgetsweb(gw_url2)
print(f"  Result: {len(links2)} links")
for lk in links2[:5]:
    print(f"    [{lk['label'][:25]}] -> {lk['url'][:55]}")
if len(links2) > 5:
    print(f"    ... +{len(links2)-5} more")

print(f"\n{'='*60}")
if links1 and links2:
    print("PASS - gadgetsweb.xyz resolution working!")
else:
    print("FAIL - some links not resolved")
