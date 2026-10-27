"""Data models shared across stages. Each one maps to a cached JSON file in data/<video_id>/."""

from typing import Literal

from pydantic import BaseModel, Field


# --- Stage 1: metadata ---------------------------------------------------------

class Chapter(BaseModel):
    start_seconds: int
    title: str


class VideoMetadata(BaseModel):
    video_id: str
    title: str
    description: str
    published_at: str  # ISO 8601 from the API, e.g. 2025-03-01T14:00:00Z
    duration_iso: str  # ISO 8601 duration, e.g. PT14M3S
    duration_seconds: int
    tags: list[str] = []
    channel_title: str
    channel_id: str
    thumbnail_url: str
    chapters: list[Chapter] = []
    github_urls: list[str] = []

    @property
    def watch_url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.video_id}"

    @property
    def embed_url(self) -> str:
        return f"https://www.youtube.com/embed/{self.video_id}"

    @property
    def channel_url(self) -> str:
        return f"https://www.youtube.com/channel/{self.channel_id}"


# --- Stages 2 and 3: transcripts ------------------------------------------------

class Segment(BaseModel):
    start: float
    end: float
    text: str


class Transcript(BaseModel):
    method: str  # captions | whisper
    language: str
    is_generated: bool | None = None
    segments: list[Segment]


# --- Stage 4: article (LLM output) ----------------------------------------------

class CodeBlock(BaseModel):
    language: str = Field(description="typescript, javascript, html, bash, ...")
    code: str
    source: Literal["video", "repo"] = "video"
    needs_verification: bool = True


class Section(BaseModel):
    heading: str
    body_html: str = Field(description="Paragraphs and lists only. Code goes in code_blocks.")
    start_seconds: int = Field(description="Where this topic begins in the video.")
    code_blocks: list[CodeBlock] = []


class FAQItem(BaseModel):
    question: str
    answer_html: str


class InternalLink(BaseModel):
    title: str
    slug: str
    anchor_text: str


class Article(BaseModel):
    title: str = Field(description="A reader-facing article title, not just the video title.")
    slug: str
    meta_title: str = Field(description="At most 60 characters.")
    meta_description: str = Field(description="120 to 155 characters.")
    excerpt: str = Field(description="At most 300 characters.")
    intro_html: str
    learning_points: list[str]
    sections: list[Section]
    key_takeaways: list[str]
    faq: list[FAQItem] = []
    tags: list[str]
    internal_links: list[InternalLink] = []
