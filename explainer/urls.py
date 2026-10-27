"""Stage 0 helper: extract a video ID from any common YouTube URL shape."""

import re
from urllib.parse import parse_qs, urlparse

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def parse_video_id(url_or_id: str) -> str:
    value = url_or_id.strip()
    if _ID_RE.match(value):
        return value

    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in parsed.path.split("/") if p]

    candidate = None
    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host in ("youtube.com", "music.youtube.com", "youtube-nocookie.com"):
        if parts[:1] == ["watch"]:
            candidate = parse_qs(parsed.query).get("v", [None])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "embed", "live", "v"):
            candidate = parts[1]

    if candidate and _ID_RE.match(candidate):
        return candidate
    raise ValueError(f"Could not find a YouTube video ID in: {url_or_id}")
