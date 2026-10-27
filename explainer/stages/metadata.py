"""Stage 1: video metadata from the YouTube Data API v3 -> metadata.json"""

import logging
import re

from explainer import net
from explainer.context import Context
from explainer.models import Chapter, VideoMetadata

log = logging.getLogger(__name__)

OUTPUT = "metadata.json"
API_URL = "https://www.googleapis.com/youtube/v3/videos"

_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)
_CHAPTER_RE = re.compile(r"^\s*[\[(]?((?:\d{1,2}:)?\d{1,2}:\d{2})[\])]?\s*(?:[-–—:|]\s*)?(.+?)\s*$")
_GITHUB_RE = re.compile(r"https?://(?:www\.)?github\.com/[\w.-]+/[\w.-]+")
_THUMBNAIL_ORDER = ["maxres", "standard", "high", "medium", "default"]


def parse_iso_duration(value: str) -> int:
    match = _DURATION_RE.match(value)
    if not match or value in ("P", "PT"):
        raise ValueError(f"Not an ISO 8601 duration: {value!r}")
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    return int(parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"])


def parse_timestamp(value: str) -> int:
    seconds = 0
    for part in value.split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def parse_chapters(description: str) -> list[Chapter]:
    chapters = []
    for line in description.splitlines():
        match = _CHAPTER_RE.match(line)
        if match:
            chapters.append(Chapter(start_seconds=parse_timestamp(match.group(1)), title=match.group(2)))
    # YouTube only treats a list as chapters if it starts at 0:00; anything else is
    # probably an incidental timestamp, so drop it.
    if not chapters or chapters[0].start_seconds != 0:
        return []
    return chapters


def extract_github_urls(text: str) -> list[str]:
    urls = []
    for match in _GITHUB_RE.findall(text):
        url = match.rstrip(".,)").removesuffix(".git")
        if url not in urls:
            urls.append(url)
    return urls


def build_metadata(item: dict) -> VideoMetadata:
    snippet = item["snippet"]
    thumbnails = snippet.get("thumbnails", {})
    thumbnail = next((thumbnails[k]["url"] for k in _THUMBNAIL_ORDER if k in thumbnails), "")
    duration_iso = item["contentDetails"]["duration"]
    description = snippet.get("description", "")
    return VideoMetadata(
        video_id=item["id"],
        title=snippet["title"],
        description=description,
        published_at=snippet["publishedAt"],
        duration_iso=duration_iso,
        duration_seconds=parse_iso_duration(duration_iso),
        tags=snippet.get("tags", []),
        channel_title=snippet["channelTitle"],
        channel_id=snippet["channelId"],
        thumbnail_url=thumbnail,
        chapters=parse_chapters(description),
        github_urls=extract_github_urls(description),
    )


def run(ctx: Context) -> None:
    ctx.settings.require("youtube_api_key")
    response = net.request(
        "GET",
        API_URL,
        params={"part": "snippet,contentDetails", "id": ctx.video_id, "key": ctx.settings.youtube_api_key},
    )
    items = response.json().get("items", [])
    if not items:
        raise RuntimeError(f"YouTube returned no video for id {ctx.video_id} (private, deleted or wrong id?)")
    metadata = build_metadata(items[0])
    ctx.save_model(OUTPUT, metadata)
    log.info("Metadata: %r, %ss, %d chapters, %d GitHub URLs",
             metadata.title, metadata.duration_seconds, len(metadata.chapters), len(metadata.github_urls))
