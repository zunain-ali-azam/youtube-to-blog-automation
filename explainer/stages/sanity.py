"""Destination stage: create or update the Sanity draft -> sanity_post.json

Optional: skipped unless DESTINATION=sanity and SANITY_PROJECT_ID and SANITY_API_TOKEN are set.

The post matches the `post` type of Sanity's official Next.js blog template (title, slug,
content, excerpt, coverImage, date), plus seo, tags, video and jsonLd fields. The schema
for those, and for the `code` and `youtube` blocks in the body, is in cms/sanity/.
SANITY_FIELDS renames fields for a Studio that names them differently.

The body is Portable Text, converted from the article's small HTML subset, so editors get
a normal rich-text field. The draft is a document with a `drafts.` id, which the Studio
shows as an unpublished draft.

When the job already has a draft, the stage patches it instead of creating another, but
only while it's unpublished and still at the revision the last push left. Otherwise the
update would throw away a reviewer's work. The cover image and date stay as they are.
"""

import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timezone
from html import escape
from html.parser import HTMLParser
from mimetypes import guess_type
from urllib.parse import urlparse

import httpx

from explainer import net
from explainer.config import ConfigError, Settings
from explainer.context import Context
from explainer.models import Article, VideoMetadata
from explainer.stages import generate, metadata as metadata_stage, render

log = logging.getLogger(__name__)

OUTPUT = "sanity_post.json"
DRAFT_PREFIX = "drafts."


# --- HTML -> Portable Text ------------------------------------------------------

def _key() -> str:
    return secrets.token_hex(6)


_DECORATORS = {"strong": "strong", "b": "strong", "em": "em", "i": "em", "code": "code"}
_STYLES = {"p": "normal", "h2": "h2", "h3": "h3", "h4": "h4"}


class _PortableText(HTMLParser):
    """Converts the article's HTML subset (p, h2-h4, ul, ol, li, strong, b, em, i, code,
    a, br) to Portable Text blocks. Unknown tags are dropped and their text kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.block: dict | None = None
        self.lists: list[str] = []  # "bullet" or "number" per open list
        self.inline: list[tuple[str, str]] = []  # (tag, mark) per open inline tag

    def _open(self, style: str = "normal") -> dict:
        self._close()
        self.block = {"_type": "block", "_key": _key(), "style": style, "markDefs": [], "children": []}
        if self.lists:
            self.block["listItem"] = self.lists[-1]
            self.block["level"] = len(self.lists)
        return self.block

    def _close(self) -> None:
        block, self.block = self.block, None
        if not block:
            return
        children = block["children"]
        if children:
            children[0]["text"] = children[0]["text"].lstrip()
            children[-1]["text"] = children[-1]["text"].rstrip()
        block["children"] = [span for span in children if span["text"]]
        if block["children"]:
            self.blocks.append(block)

    def handle_starttag(self, tag, attrs):
        if tag in _STYLES:
            self._open(_STYLES[tag])
        elif tag in ("ul", "ol"):
            self._close()
            self.lists.append("bullet" if tag == "ul" else "number")
        elif tag == "li":
            self._open()
        elif tag in _DECORATORS:
            self.inline.append((tag, _DECORATORS[tag]))
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                key = _key()
                # linkType: the template's link annotation shows href only when it is "href".
                (self.block or self._open())["markDefs"].append(
                    {"_key": key, "_type": "link", "linkType": "href", "href": href})
                self.inline.append((tag, key))
            else:
                self.inline.append((tag, ""))
        elif tag == "br":
            self._text("\n", collapse=False)

    def handle_endtag(self, tag):
        if tag in _STYLES or tag == "li":
            self._close()
        elif tag in ("ul", "ol") and self.lists:
            self._close()
            self.lists.pop()
        elif tag in _DECORATORS or tag == "a":
            for i in range(len(self.inline) - 1, -1, -1):
                if self.inline[i][0] == tag:
                    del self.inline[i]
                    break

    def handle_data(self, data):
        self._text(data)

    def _text(self, data: str, collapse: bool = True) -> None:
        if collapse:
            data = re.sub(r"\s+", " ", data)
        if not self.block and not data.strip():
            return  # whitespace between blocks
        block = self.block or self._open()
        children = block["children"]
        if collapse and (not children or children[-1]["text"].endswith((" ", "\n"))):
            data = data.lstrip()
        if not data:
            return
        marks = [mark for _, mark in self.inline if mark]
        if children and children[-1]["marks"] == marks:
            children[-1]["text"] += data
        else:
            children.append({"_type": "span", "_key": _key(), "text": data, "marks": marks})

    def result(self) -> list[dict]:
        self._close()
        return self.blocks


def html_to_portable_text(html: str) -> list[dict]:
    parser = _PortableText()
    parser.feed(html)
    parser.close()
    return parser.result()


def _list_html(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def build_content(article: Article, meta: VideoMetadata, links: list[tuple[str, str]] = ()) -> list[dict]:
    """The article body as Portable Text, in the same order as render.build_html."""
    pt = html_to_portable_text
    blocks = pt(article.intro_html)
    if article.learning_points:
        blocks += pt("<h2>What you'll learn</h2>" + _list_html(article.learning_points))
    blocks.append({"_type": "youtube", "_key": _key(), "url": meta.watch_url})

    for section in article.sections:
        blocks += pt(f"<h2>{escape(section.heading)}</h2>" + render.watch_link(meta, section.start_seconds))
        blocks += pt(section.body_html)
        blocks += [{"_type": "code", "_key": _key(), "language": block.language.lower().strip() or "text",
                    "code": block.code} for block in section.code_blocks]

    if article.key_takeaways:
        blocks += pt("<h2>Key takeaways</h2>" + _list_html(article.key_takeaways))
    if article.faq:
        blocks += pt("<h2>Frequently asked questions</h2>")
        for item in article.faq:
            blocks += pt(f"<h3>{escape(item.question)}</h3>" + item.answer_html)
    if links:
        blocks += pt(render.related_html(links))
    blocks += pt(render.credit_html(meta))
    return blocks


# --- document -------------------------------------------------------------------

def parse_field_map(spec: str) -> dict[str, str | None]:
    """SANITY_FIELDS ("content=body,seo=-") -> {"content": "body", "seo": None}."""
    mapping = {}
    for pair in filter(None, (part.strip() for part in spec.split(","))):
        name, sep, target = (p.strip() for p in pair.partition("="))
        if not sep or not name or not target:
            raise ConfigError(f"SANITY_FIELDS entry {pair!r} should look like field=newName or field=-")
        mapping[name] = None if target == "-" else target
    return mapping


def apply_field_map(fields: dict, mapping: dict[str, str | None]) -> dict:
    result = {}
    for name, value in fields.items():
        target = mapping.get(name, name)
        if target is not None:
            result[target] = value
    return result


def build_fields(article: Article, meta: VideoMetadata, schemas: list[dict], content: list[dict],
                 settings: Settings) -> dict:
    """Every field the stage sets, before SANITY_FIELDS renames. Sent on create and update."""
    seo = {"metaTitle": article.meta_title, "metaDescription": article.meta_description}
    if settings.site_url:
        seo["canonicalUrl"] = f"{settings.site_url}/{article.slug}"
    return {
        "title": article.title,
        "slug": {"_type": "slug", "current": article.slug},
        "excerpt": article.excerpt,
        "content": content,
        "seo": seo,
        "tags": article.tags,
        "video": {"url": meta.watch_url, "title": meta.title, "publishedAt": meta.published_at},
        # A string: the front end puts it into <script type="application/ld+json"> as is.
        "jsonLd": json.dumps(schemas, ensure_ascii=False),
    }


def cover_image(asset_id: str, alt: str) -> dict:
    return {"_type": "image", "asset": {"_type": "reference", "_ref": asset_id}, "alt": alt}


# --- HTTP API -------------------------------------------------------------------

class SanityConflict(Exception):
    pass


class SanityClient:
    def __init__(self, settings: Settings):
        settings.require("sanity_project_id", "sanity_dataset", "sanity_api_token")
        self.base = f"https://{settings.sanity_project_id}.api.sanity.io/{settings.sanity_api_version}"
        self.dataset = settings.sanity_dataset
        self.studio = settings.sanity_studio_url
        self.post_type = settings.sanity_post_type
        self._token = settings.sanity_api_token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._token}"}

    def mutate(self, mutations: list[dict]) -> dict:
        try:
            response = net.request("POST", f"{self.base}/data/mutate/{self.dataset}", headers=self._headers(),
                                   json={"mutations": mutations})
        except httpx.HTTPStatusError as exc:
            # ifRevisionID didn't match: someone saved the draft in between.
            if exc.response.status_code == 409:
                raise SanityConflict("The Sanity draft changed while it was being updated; run again") from exc
            raise
        return response.json()

    def versions(self, doc_id: str) -> dict[str, dict | None]:
        """The draft and published documents for an id: {"draft": {_id, _rev} | None, "published": ...}."""
        draft_id = DRAFT_PREFIX + doc_id
        response = net.request(
            "GET",
            f"{self.base}/data/query/{self.dataset}",
            headers=self._headers(),
            params={"query": "*[_id in $ids]{_id, _rev}", "$ids": json.dumps([draft_id, doc_id]),
                    "perspective": "raw"},
        )
        found = {doc["_id"]: doc for doc in response.json()["result"]}
        return {"draft": found.get(draft_id), "published": found.get(doc_id)}

    def upload_image(self, data: bytes, filename: str) -> str:
        response = net.request(
            "POST",
            f"{self.base}/assets/images/{self.dataset}",
            headers={**self._headers(), "Content-Type": guess_type(filename)[0] or "image/jpeg"},
            params={"filename": filename},
            content=data,
        )
        return response.json()["document"]["_id"]

    def editor_url(self, doc_id: str) -> str | None:
        if not self.studio:
            return None
        return f"{self.studio}/intent/edit/id={doc_id};type={self.post_type}"


def check_updatable(versions: dict[str, dict | None], last_rev: str | None) -> None:
    """Raise SanityConflict unless the post is unpublished and its draft unchanged since our last push."""
    if versions["published"]:
        raise SanityConflict("The Sanity post has been published, so it won't be overwritten. "
                             "Use `yt2blog run --force` to create a new draft instead")
    if versions["draft"]["_rev"] != last_rev:
        raise SanityConflict(
            "The Sanity draft was edited after the last push, so updating it would lose those edits. "
            "Use `yt2blog run --force` to create a new draft instead"
        )


# --- stage ----------------------------------------------------------------------

def is_configured(settings: Settings) -> bool:
    return bool(settings.sanity_project_id and settings.sanity_api_token)


def run(ctx: Context) -> None:
    if ctx.destination != "sanity":
        log.info("Not sending this run to Sanity, skipping the draft")
        return
    if not is_configured(ctx.settings):
        log.info("Sanity is not configured (SANITY_PROJECT_ID or SANITY_API_TOKEN is empty), skipping the draft")
        return
    mapping = parse_field_map(ctx.settings.sanity_fields)
    sanity = SanityClient(ctx.settings)
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    schemas = ctx.load_json(render.JSONLD_OUTPUT)
    posts = ctx.load_json(generate.POSTS_OUTPUT) if ctx.path(generate.POSTS_OUTPUT).exists() else []
    content = build_content(article, meta, render.resolve_links(article, posts))
    fields = build_fields(article, meta, schemas, content, ctx.settings)

    doc_id = (ctx.jobs.get(ctx.video_id) or {}).get("sanity_document_id")
    if doc_id:
        versions = sanity.versions(doc_id)
        if versions["draft"] or versions["published"]:
            last_rev = ctx.load_json(OUTPUT).get("rev") if ctx.path(OUTPUT).exists() else None
            check_updatable(versions, last_rev)
            sanity.mutate([{"patch": {"id": DRAFT_PREFIX + doc_id, "ifRevisionID": last_rev,
                                      "set": apply_field_map(fields, mapping)}}])
            _save(ctx, sanity, doc_id)
            log.info("Sanity draft updated: %s", DRAFT_PREFIX + doc_id)
            return
        log.warning("Sanity document %s no longer exists, creating a new draft", doc_id)

    doc_id = str(uuid.uuid4())
    document = {**fields, "date": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if meta.thumbnail_url:
        image = net.request("GET", meta.thumbnail_url).content
        filename = f"{ctx.video_id}-{urlparse(meta.thumbnail_url).path.rsplit('/', 1)[-1]}"
        document["coverImage"] = cover_image(sanity.upload_image(image, filename), meta.title)
    sanity.mutate([{"create": {"_id": DRAFT_PREFIX + doc_id, "_type": sanity.post_type,
                               **apply_field_map(document, mapping)}}])
    _save(ctx, sanity, doc_id)
    log.info("Sanity draft created: %s", DRAFT_PREFIX + doc_id)


def _save(ctx: Context, sanity: SanityClient, doc_id: str) -> None:
    # rev is what the next run compares against to spot edits made in the Studio.
    draft = sanity.versions(doc_id)["draft"]
    editor_url = sanity.editor_url(doc_id)
    ctx.save_json(OUTPUT, {"id": doc_id, "draft_id": DRAFT_PREFIX + doc_id, "editor_url": editor_url,
                           "rev": draft["_rev"] if draft else None})
    ctx.jobs.upsert(ctx.video_id, sanity_document_id=doc_id, sanity_editor_url=editor_url)
