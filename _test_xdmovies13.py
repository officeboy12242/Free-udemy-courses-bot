import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from curl_cffi import requests

url_movie = "https://top.xdmovies.wtf/movies/dhurandhar-the-revenge-2160p-1080p-hindi-tamil-download-1582770"

for imp in ["chrome110", "chrome116", "chrome120", "safari15_3", "safari17_0", "edge101"]:
    try:
        r = requests.get(url_movie, impersonate=imp, timeout=10)
        print(f"{imp}: {r.status_code}")
    except Exception as e:
        print(f"{imp}: {e}")
