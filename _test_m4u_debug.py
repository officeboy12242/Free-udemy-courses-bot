import sys
sys.stdout.reconfigure(encoding='utf-8')

from movie_service import _get, M4U_BASE
from bs4 import BeautifulSoup

print(f"Testing Movies4U at {M4U_BASE}")
print("="*60)

try:
    resp = _get(M4U_BASE + "/", timeout=15)
    print(f"Status: {resp.status_code}")
    print(f"Content length: {len(resp.text)}")
    print("\nFirst 500 chars of HTML:")
    print(resp.text[:500])

    soup = BeautifulSoup(resp.text, "html.parser")

    # Check for article tags
    articles = soup.select("article")
    print(f"\nFound {len(articles)} article tags")

    # Check for common movie container patterns
    patterns = [
        ("article", soup.select("article")),
        (".post", soup.select(".post")),
        (".movie-item", soup.select(".movie-item")),
        (".entry-title", soup.select(".entry-title")),
        ("h2 a", soup.select("h2 a")),
        ("h3 a", soup.select("h3 a")),
        (".post-title", soup.select(".post-title")),
        (".entry-title a", soup.select(".entry-title a")),
    ]

    print("\n" + "="*60)
    print("Testing selectors:")
    for name, elements in patterns:
        print(f"  {name}: {len(elements)} found")
        if elements and len(elements) > 0:
            print(f"    First: {elements[0].get_text(strip=True)[:50]}...")

except Exception as e:
    print(f"ERROR: {type(e).__name__}: {e}")
