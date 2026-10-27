"""Stage 5: create or update the Ghost draft -> ghost_post.json

Optional: when GHOST_ADMIN_API_KEY is not set, the stage is skipped and the job
ends with article.md as its result.

Uses the Ghost Admin API: a fresh HS256 JWT per request, the thumbnail uploaded as
the feature image, and the post created from HTML (`?source=html`) as a draft.

When the job already has a post, the stage updates it instead of creating another,
but only while it is still a draft nobody has edited in Ghost since the last push.
Otherwise the update would throw away a reviewer's work.

Also holds the Content API client that lists published posts for internal links.
"""

import logging
import time
from mimetypes import guess_type
from urllib.parse import urlparse

import httpx
import jwt

from explainer import net
from explainer.config import Settings
from explainer.context import Context
from explainer.models import Article, VideoMetadata
from explainer.stages import generate, metadata as metadata_stage, render

log = logging.getLogger(__name__)

OUTPUT = "ghost_post.json"
ACCEPT_VERSION = "v5.0"


def admin_token(admin_api_key: str, now: int | None = None) -> str:
    key_id, _, secret = admin_api_key.partition(":")
    if not key_id or not secret:
        raise ValueError("GHOST_ADMIN_API_KEY must look like <id>:<secret>")
    iat = int(now if now is not None else time.time())
    return jwt.encode(
        {"iat": iat, "exp": iat + 5 * 60, "aud": "/admin/"},
        bytes.fromhex(secret),
        algorithm="HS256",
        headers={"kid": key_id},
    )


class GhostAdmin:
    def __init__(self, settings: Settings):
        settings.require("ghost_url", "ghost_admin_api_key")
        self.base = f"{settings.ghost_url}/ghost/api/admin"
        self.site = settings.ghost_url
        self._key = settings.ghost_admin_api_key

    def _headers(self) -> dict:
        return {"Authorization": f"Ghost {admin_token(self._key)}", "Accept-Version": ACCEPT_VERSION}

    def upload_image(self, data: bytes, filename: str) -> str:
        content_type = guess_type(filename)[0] or "image/jpeg"
        response = net.request(
            "POST",
            f"{self.base}/images/upload/",
            headers=self._headers(),
            files={"file": (filename, data, content_type)},
            data={"purpose": "image", "ref": filename},
        )
        return response.json()["images"][0]["url"]

    def create_post(self, post: dict) -> dict:
        response = net.request(
            "POST",
            f"{self.base}/posts/",
            params={"source": "html"},
            headers=self._headers(),
            json={"posts": [post]},
        )
        return response.json()["posts"][0]

    def get_post(self, post_id: str) -> dict | None:
        try:
            response = net.request("GET", f"{self.base}/posts/{post_id}/", headers=self._headers())
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return response.json()["posts"][0]

    def update_post(self, post_id: str, post: dict) -> dict:
        # Ghost rejects an edit unless updated_at matches the stored post.
        response = net.request(
            "PUT",
            f"{self.base}/posts/{post_id}/",
            params={"source": "html"},
            headers=self._headers(),
            json={"posts": [post]},
        )
        return response.json()["posts"][0]

    def editor_url(self, post_id: str) -> str:
        return f"{self.site}/ghost/#/editor/post/{post_id}"


def build_tags(primary: str, tags: list[str]) -> list[str]:
    """Primary tag first, then the article's tags, without case-insensitive duplicates."""
    result, seen = [], set()
    for tag in [primary, *tags]:
        tag = tag.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            result.append(tag)
    return result


def build_post(article: Article, html: str, codeinjection_head: str, settings: Settings,
               feature_image: str | None) -> dict:
    post = {
        "title": article.title,
        "slug": article.slug,
        "html": html,
        "status": "draft",
        "custom_excerpt": article.excerpt[:300],
        "meta_title": article.meta_title,
        "meta_description": article.meta_description,
        "tags": build_tags(settings.ghost_primary_tag, article.tags),
        "codeinjection_head": codeinjection_head,
        # A headless front end has no theme to fall back to the meta fields, so fill these in.
        "og_title": article.meta_title,
        "og_description": article.meta_description,
        "twitter_title": article.meta_title,
        "twitter_description": article.meta_description,
    }
    if settings.site_url:
        post["canonical_url"] = f"{settings.site_url}/{article.slug}"
    if feature_image:
        post["feature_image"] = feature_image
    if settings.ghost_author_email:
        post["authors"] = [settings.ghost_author_email]
    return post


def is_configured(settings: Settings) -> bool:
    return bool(settings.ghost_admin_api_key)


def fetch_published_posts(settings: Settings) -> list[dict]:
    """Published posts from the Content API: title, slug, url and tag names."""
    response = net.request(
        "GET",
        f"{settings.ghost_url}/ghost/api/content/posts/",
        params={"key": settings.ghost_content_api_key, "limit": "all", "fields": "title,slug,url", "include": "tags"},
        headers={"Accept-Version": ACCEPT_VERSION},
    )
    return [
        {"title": p["title"], "slug": p["slug"], "url": p["url"], "tags": [t["name"] for t in p.get("tags", [])]}
        for p in response.json()["posts"]
    ]


class GhostConflict(Exception):
    pass


def check_updatable(current: dict, last_pushed_at: str | None) -> None:
    """Raise GhostConflict unless the post is a draft that hasn't changed since our last push."""
    if current["status"] != "draft":
        raise GhostConflict(f"The Ghost post is {current['status']}, not a draft; it won't be overwritten")
    if current["updated_at"] != last_pushed_at:
        raise GhostConflict(
            "The Ghost draft was edited after the last push, so updating it would lose those edits. "
            "Use `explainer run --force` to create a new draft instead"
        )


def _existing_post_id(ctx: Context) -> str | None:
    job = ctx.jobs.get(ctx.video_id) or {}
    return job.get("ghost_post_id")


def run(ctx: Context) -> None:
    if not is_configured(ctx.settings):
        log.info("Ghost is not configured (GHOST_ADMIN_API_KEY is empty), skipping the draft")
        return
    ghost = GhostAdmin(ctx.settings)
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    html = ctx.path(render.HTML_OUTPUT).read_text(encoding="utf-8")
    jsonld = "\n".join(render.jsonld_script(schema) for schema in ctx.load_json(render.JSONLD_OUTPUT))

    post_id = _existing_post_id(ctx)
    current = ghost.get_post(post_id) if post_id else None
    if post_id and current is None:
        log.warning("Ghost post %s no longer exists, creating a new draft", post_id)
    if current:
        last_pushed_at = ctx.load_json(OUTPUT).get("updated_at") if ctx.path(OUTPUT).exists() else None
        check_updatable(current, last_pushed_at)
        # Keep the feature image and authors already on the post.
        post = build_post(article, html, jsonld, ctx.settings, feature_image=None)
        post.pop("authors", None)
        post["updated_at"] = current["updated_at"]
        _save(ctx, ghost, ghost.update_post(post_id, post))
        log.info("Ghost draft updated: %s", ghost.editor_url(post_id))
        return

    feature_image = None
    if meta.thumbnail_url:
        image = net.request("GET", meta.thumbnail_url).content
        filename = f"{ctx.video_id}-{urlparse(meta.thumbnail_url).path.rsplit('/', 1)[-1]}"
        feature_image = ghost.upload_image(image, filename)

    created = ghost.create_post(build_post(article, html, jsonld, ctx.settings, feature_image))
    _save(ctx, ghost, created)
    log.info("Ghost draft created: %s", ghost.editor_url(created["id"]))


def _save(ctx: Context, ghost: GhostAdmin, post: dict) -> None:
    # updated_at is what the next run compares against to spot edits made in Ghost.
    editor_url = ghost.editor_url(post["id"])
    ctx.save_json(OUTPUT, {"id": post["id"], "slug": post.get("slug"), "editor_url": editor_url,
                           "updated_at": post["updated_at"]})
    ctx.jobs.upsert(ctx.video_id, ghost_post_id=post["id"], ghost_editor_url=editor_url)
