import sys, os
sys.stdout.reconfigure(encoding='utf-8')

with open(r"c:\Users\jaikishanbagul\Downloads\tgbot2\movie_service.py", "a", encoding="utf-8") as f:
    f.write("""

def format_moviesmod_message(movie_title: str, data: dict, footer: bool = True) -> str:
    \"\"\"Format a MoviesMod result as HTML for Telegram.\"\"\"
    links = data.get("links", [])
    
    lines: list[str] = []
    disp = movie_title or data.get("title", "")
    if disp:
        lines.append(f"🎬 <b>{disp}</b>")
        
    if not links:
        if lines:
            lines.append("\\n❌ No download links parsed.")
        return "\\n".join(lines) if lines else ""
        
    lines.append("━" * 32)
    lines.append("📥 <b>Download Links (MoviesMod)</b>\\n")
    
    for lk in links:
        label = lk.get("label", "Download")
        url_ = lk.get("url", "")
        
        lines.append(f"📦 <b>{label}</b>")
        
        # If there are multiple links separated by newline (e.g. Instant Download and Telegram File)
        parts = []
        for u in url_.split('\\n'):
            if 'tgseed.link' in u:
                parts.append(f"<a href='{u}'>Telegram File</a>")
            elif 'video-seed.pro' in u or 'cdn.video-gen.xyz' in u:
                parts.append(f"<a href='{u}'>Direct Download</a>")
            elif 'driveseed.org' in u:
                parts.append(f"<a href='{u}'>DriveSeed</a>")
            elif 'uhdmovies.foo' in u:
                parts.append(f"<a href='{u}'>UHDMovies</a>")
            else:
                parts.append(f"<a href='{u}'>Download</a>")
                
        lines.append("   🔗 " + " · ".join(parts))
        lines.append("")
        
    if footer:
        lines.append("━" * 32)
        lines.append("⚡ <a href='https://t.me/CoursesDrivee'>Powered by @CoursesDrivee</a>")
        
    return "\\n".join(lines)
""")
