import pytest

from explainer.stages.generate import chunk_segments
from explainer.stages.metadata import (
    build_metadata,
    extract_github_urls,
    parse_chapters,
    parse_iso_duration,
)
from explainer.stages.transcript import to_segments
from explainer.models import Segment
from explainer.urls import parse_video_id

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    VID,
    f"https://www.youtube.com/watch?v={VID}",
    f"https://youtube.com/watch?v={VID}&t=42s&list=PL123",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://youtu.be/{VID}",
    f"https://youtu.be/{VID}?si=abc",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/live/{VID}",
    f"youtube.com/watch?v={VID}",
])
def test_parse_video_id(url):
    assert parse_video_id(url) == VID


@pytest.mark.parametrize("url", ["https://example.com/watch?v=" + VID, "https://youtu.be/short", "not a url"])
def test_parse_video_id_rejects(url):
    with pytest.raises(ValueError):
        parse_video_id(url)


@pytest.mark.parametrize("value,expected", [
    ("PT14M3S", 843),
    ("PT1H2M15S", 3735),
    ("PT45S", 45),
    ("PT2H", 7200),
    ("P1DT1S", 86401),
    ("P0D", 0),
])
def test_parse_iso_duration(value, expected):
    assert parse_iso_duration(value) == expected


@pytest.mark.parametrize("value", ["", "PT", "14:03", "P1W"])
def test_parse_iso_duration_rejects(value):
    with pytest.raises(ValueError):
        parse_iso_duration(value)


def test_parse_chapters():
    description = """Learn Signals in Angular.

0:00 Intro
1:02 - Creating a signal
(12:30) computed() and effect()
1:02:15 Deploying

Code: https://github.com/codewithahsan/signals-demo"""
    chapters = parse_chapters(description)
    assert [(c.start_seconds, c.title) for c in chapters] == [
        (0, "Intro"),
        (62, "Creating a signal"),
        (750, "computed() and effect()"),
        (3735, "Deploying"),
    ]


def test_parse_chapters_requires_zero_start():
    assert parse_chapters("Jump to 4:32 for the demo\n5:00 Outro") == []


def test_extract_github_urls():
    text = "Repo: https://github.com/owner/repo.git, also (https://github.com/owner/repo) and https://github.com/a/b."
    assert extract_github_urls(text) == ["https://github.com/owner/repo", "https://github.com/a/b"]


def test_build_metadata_from_api_item():
    item = {
        "id": VID,
        "snippet": {
            "title": "Angular Signals",
            "description": "0:00 Intro\n2:00 Demo",
            "publishedAt": "2025-03-01T14:00:00Z",
            "channelTitle": "Code with Ahsan",
            "channelId": "UC123",
            "tags": ["angular"],
            "thumbnails": {"high": {"url": "https://i.ytimg.com/hq.jpg"}, "maxres": {"url": "https://i.ytimg.com/max.jpg"}},
        },
        "contentDetails": {"duration": "PT10M"},
    }
    meta = build_metadata(item)
    assert meta.thumbnail_url == "https://i.ytimg.com/max.jpg"
    assert meta.duration_seconds == 600
    assert len(meta.chapters) == 2


def test_to_segments_trims_overlap_and_whitespace():
    segments = to_segments([(0.0, 5.0, "hello\nworld"), (3.0, 4.0, "  next "), (8.0, 1.0, "   ")])
    assert [(s.start, s.end, s.text) for s in segments] == [(0.0, 3.0, "hello world"), (3.0, 7.0, "next")]


def test_chunk_segments_keeps_timestamps():
    segments = [Segment(start=i * 10, end=i * 10 + 10, text="x" * 40) for i in range(10)]
    chunks = chunk_segments(segments, max_chars=100)
    assert chunks[0].start == 0 and chunks[-1].end == 100
    assert sum(c.text.count("x") for c in chunks) == 400
    assert all(len(c.text) <= 100 for c in chunks)
