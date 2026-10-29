"""Stage 3: LLM writes the article from the metadata and raw transcript -> article.json

There is no separate cleanup pass: the model gets the raw transcript and the
glossary, and fixes misheard terms while it writes. Caption segments are only a few
seconds long, so they are merged into chunks of about a minute, each labelled with
its start time for the "watch this part" links.

The response is constrained to the `Article` JSON schema and then validated with
pydantic. If validation fails, the error is sent back and the model gets up to two
more attempts.

When the Ghost Content API key is set, the site's published posts are passed in so
the model can pick internal links, and saved to ghost_posts.json for the render stage.
"""

import json
import logging

import httpx
from pydantic import ValidationError

from explainer import llm
from explainer.config import load_glossary, load_prompt
from explainer.context import Context
from explainer.models import Article, Segment, Transcript, VideoMetadata
from explainer.stages import metadata as metadata_stage, transcript as transcript_stage
from explainer.timestamps import format_timestamp

log = logging.getLogger(__name__)

OUTPUT = "article.json"
POSTS_OUTPUT = "ghost_posts.json"
MAX_RETRIES = 2
CHUNK_CHARS = 1000


def chunk_segments(segments: list[Segment], max_chars: int = CHUNK_CHARS) -> list[Segment]:
    chunks: list[Segment] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        if current and size + len(segment.text) > max_chars:
            chunks.append(_merge(current))
            current, size = [], 0
        current.append(segment)
        size += len(segment.text) + 1
    if current:
        chunks.append(_merge(current))
    return chunks


def _merge(segments: list[Segment]) -> Segment:
    return Segment(
        start=segments[0].start,
        end=segments[-1].end,
        text=" ".join(s.text for s in segments),
    )


def build_user_message(meta: VideoMetadata, transcript: Transcript, existing_posts: list[dict]) -> str:
    video = {
        "title": meta.title,
        "channel": meta.channel_title,
        "published_at": meta.published_at,
        "duration": format_timestamp(meta.duration_seconds),
        "tags": meta.tags,
        "github_urls": meta.github_urls,
        "description": meta.description,
    }
    chapters = "\n".join(f"{format_timestamp(c.start_seconds)} {c.title}" for c in meta.chapters)
    lines = "\n\n".join(
        f"[{format_timestamp(chunk.start)} = {int(chunk.start)}s]\n{chunk.text}"
        for chunk in chunk_segments(transcript.segments)
    )
    posts = json.dumps(existing_posts, ensure_ascii=False) if existing_posts else "None yet."
    return (
        f"<video_metadata>\n{json.dumps(video, indent=2, ensure_ascii=False)}\n</video_metadata>\n\n"
        f"<chapters>\n{chapters or 'The video has no chapters.'}\n</chapters>\n\n"
        f"<existing_posts>\n{posts}\n</existing_posts>\n\n"
        f"<transcript>\n{lines}\n</transcript>\n\n"
        "Write the article for this video."
    )


def run(ctx: Context) -> None:
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    transcript = ctx.load_model(transcript_stage.OUTPUT, Transcript)
    glossary = "\n".join(load_glossary())
    system = (
        load_prompt("generate_article.md").replace("{glossary}", glossary)
        + "\n\n" + load_prompt("style_guide.md")
    )

    posts = _published_posts(ctx)
    existing = [{"title": p["title"], "slug": p["slug"], "tags": p["tags"]} for p in posts]
    messages = [{"role": "user", "content": build_user_message(meta, transcript, existing_posts=existing)}]

    for attempt in range(MAX_RETRIES + 1):
        text = llm.call(
            ctx.settings,
            system=system,
            messages=messages,
            effort="high",
            shape=Article,
        )
        try:
            article = Article.model_validate_json(text)
        except ValidationError as exc:
            if attempt == MAX_RETRIES:
                raise
            log.warning("Article failed validation (attempt %d), retrying: %s", attempt + 1, exc)
            messages.append({"role": "assistant", "content": text})
            messages.append({
                "role": "user",
                "content": f"That JSON failed validation:\n{exc}\n\nReturn the complete corrected article.",
            })
            continue
        ctx.save_model(OUTPUT, article)
        log.info("Article: %r, %d sections, %d internal links",
                 article.title, len(article.sections), len(article.internal_links))
        return


def _published_posts(ctx: Context) -> list[dict]:
    # Imported here because the ghost stage imports this module.
    from explainer.stages import ghost

    if not ctx.settings.ghost_content_api_key:
        ctx.save_json(POSTS_OUTPUT, [])
        return []
    try:
        posts = ghost.fetch_published_posts(ctx.settings)
    except httpx.HTTPError as exc:
        # Internal links are a nice-to-have; don't fail the article over them.
        log.warning("Couldn't list Ghost posts for internal links: %s", exc)
        posts = []
    ctx.save_json(POSTS_OUTPUT, posts)
    log.info("Ghost: %d published posts available for internal links", len(posts))
    return posts
