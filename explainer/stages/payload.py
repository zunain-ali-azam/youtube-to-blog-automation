"""Destination stage: create or update the Payload draft -> payload_post.json

Optional: skipped unless DESTINATION=payload and Payload credentials are set (PAYLOAD_API_KEY,
or PAYLOAD_EMAIL and PAYLOAD_PASSWORD for a user to log in as).

The post matches the `posts` collection of Payload's official website template (title,
heroImage, content, meta, slug), plus tags, video, jsonLd and canonicalUrl fields. The config
for those, and for the `youtube` block in the body, is in cms/payload/. PAYLOAD_FIELDS renames
fields for a collection that names them differently.

The body is Lexical JSON, Payload's rich-text format, converted from the article's small
HTML subset, so editors get a normal rich-text field. Code goes in the template's `code`
block. Everything is saved with ?draft=true, so the post stays an unpublished draft.

When the job already has a post, the stage saves a new draft of it instead of creating
another, but only while it's unpublished and its latest draft is still the one the last
push left (same updatedAt). Otherwise the update would throw away a reviewer's work. The
hero image stays as it is.
"""

import json
import logging
import re
import secrets
from html.parser import HTMLParser
from mimetypes import guess_type
from urllib.parse import urlparse

import httpx

from explainer import net
from explainer.config import ConfigError, Settings
from explainer.context import Context
from explainer.models import Article, VideoMetadata
from explainer.stages import cms, generate, metadata as metadata_stage, render

log = logging.getLogger(__name__)

OUTPUT = "payload_post.json"


# --- HTML -> Lexical ------------------------------------------------------------

# Lexical's text format bits.
_FORMATS = {"strong": 1, "b": 1, "em": 2, "i": 2, "code": 16}
_HEADINGS = ("h2", "h3", "h4")


def _element(node_type: str, **extra) -> dict:
    return {"type": node_type, "children": [], "direction": "ltr", "format": "", "indent": 0, "version": 1, **extra}


def _text_node(text: str, fmt: int) -> dict:
    return {"type": "text", "text": text, "format": fmt, "detail": 0, "mode": "normal", "style": "", "version": 1}


def _texts(node: dict) -> list[dict]:
    """The text nodes inside an element, links included, in order."""
    found = []
    for child in node.get("children", []):
        if child["type"] == "text":
            found.append(child)
        elif child["type"] == "link":
            found += _texts(child)
    return found


def _prune(node: dict) -> None:
    """Drop empty text nodes, then links left with no text."""
    for child in node["children"]:
        if child["type"] == "link":
            _prune(child)
    node["children"] = [child for child in node["children"]
                        if not (child["type"] == "text" and not child["text"])
                        and not (child["type"] == "link" and not child["children"])]


class _Lexical(HTMLParser):
    """Converts the article's HTML subset (p, h2-h4, ul, ol, li, strong, b, em, i, code,
    a, br) to Lexical nodes. Unknown tags are dropped and their text kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes: list[dict] = []
        self.block: dict | None = None  # the paragraph, heading or list item taking text
        self.lists: list[dict] = []
        self.formats: list[tuple[str, int]] = []
        self.link: dict | None = None

    def _open(self, node: dict) -> dict:
        self._close()
        (self.lists[-1]["children"] if self.lists else self.nodes).append(node)
        self.block = node
        return node

    def _open_text_block(self) -> dict:
        return self._open(_element("listitem", value=1) if self.lists
                          else _element("paragraph", textFormat=0, textStyle=""))

    def _close(self) -> None:
        block, self.block = self.block, None
        self.link = None
        if not block:
            return
        texts = _texts(block)
        if texts:
            texts[0]["text"] = texts[0]["text"].lstrip()
            texts[-1]["text"] = texts[-1]["text"].rstrip()
        _prune(block)
        if not block["children"]:
            parent = self.lists[-1]["children"] if self.lists else self.nodes
            parent.remove(block)

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            if not (self.lists and self.block):  # a <p> inside an <li> stays part of the item
                self._open(_element("paragraph", textFormat=0, textStyle=""))
        elif tag in _HEADINGS:
            self._open(_element("heading", tag=tag))
        elif tag in ("ul", "ol"):
            self._close()
            listing = _element("list", listType="bullet" if tag == "ul" else "number", start=1,
                               tag="ul" if tag == "ul" else "ol")
            if self.lists:
                # Lexical nests a list in a list item of its own.
                wrapper = _element("listitem", value=1)
                wrapper["children"].append(listing)
                self.lists[-1]["children"].append(wrapper)
            else:
                self.nodes.append(listing)
            self.lists.append(listing)
        elif tag == "li":
            self._open(_element("listitem", value=1))
        elif tag in _FORMATS:
            self.formats.append((tag, _FORMATS[tag]))
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                block = self.block or self._open_text_block()
                self.link = {**_element("link"), "version": 3,
                             "fields": {"linkType": "custom", "url": href, "newTab": False}}
                block["children"].append(self.link)
        elif tag == "br":
            (self.link or self.block or self._open_text_block())["children"].append({"type": "linebreak", "version": 1})

    def handle_endtag(self, tag):
        if tag in _HEADINGS or tag == "li" or (tag == "p" and not self.lists):
            self._close()
        elif tag in ("ul", "ol") and self.lists:
            self._close()
            self.lists.pop()
        elif tag == "a":
            self.link = None
        elif tag in _FORMATS:
            for i in range(len(self.formats) - 1, -1, -1):
                if self.formats[i][0] == tag:
                    del self.formats[i]
                    break

    def handle_data(self, data):
        data = re.sub(r"\s+", " ", data)
        if not self.block and not data.strip():
            return  # whitespace between blocks
        block = self.block or self._open_text_block()
        texts = _texts(block)
        if not texts or texts[-1]["text"].endswith(" "):
            data = data.lstrip()
        if not data:
            return
        fmt = 0
        for _, bit in self.formats:
            fmt |= bit
        children = (self.link or block)["children"]
        if children and children[-1]["type"] == "text" and children[-1]["format"] == fmt:
            children[-1]["text"] += data
        else:
            children.append(_text_node(data, fmt))

    def result(self) -> list[dict]:
        self._close()
        _number(self.nodes)
        return self.nodes


def _number(nodes: list[dict]) -> None:
    """Set each list item's `value` to its position, as Lexical does."""
    for node in nodes:
        if node["type"] == "list":
            for i, item in enumerate(node["children"], start=1):
                item["value"] = i
        if "children" in node:
            _number(node["children"])


def html_to_lexical(html: str) -> list[dict]:
    parser = _Lexical()
    parser.feed(html)
    parser.close()
    return parser.result()


def _block(block_type: str, **fields) -> dict:
    # id is what Payload generates for a block: a MongoDB-style ObjectID.
    return {"type": "block", "version": 2, "format": "",
            "fields": {"id": secrets.token_hex(12), "blockName": "", "blockType": block_type, **fields}}


def build_content(article: Article, meta: VideoMetadata, links: list[tuple[str, str]] = ()) -> dict:
    """The article body as a Lexical editor state, in the order of cms.body_parts."""
    nodes = []
    for kind, *value in cms.body_parts(article, meta, links):
        if kind == "html":
            nodes += html_to_lexical(value[0])
        elif kind == "youtube":
            nodes.append(_block("youtube", url=value[0]))
        else:
            language, code = value
            nodes.append(_block("code", language=language or "plaintext", code=code))
    root = _element("root")
    root["children"] = nodes
    return {"root": root}


# --- document -------------------------------------------------------------------

def build_fields(article: Article, meta: VideoMetadata, schemas: list[dict], content: dict,
                 settings: Settings) -> dict:
    """Every field the stage sets, before PAYLOAD_FIELDS renames. Sent on create and update."""
    fields = {
        "title": article.title,
        "slug": article.slug,
        "content": content,
        # The SEO plugin's group. meta.image is set on create only, like the hero image.
        "meta": {"title": article.meta_title, "description": article.meta_description},
        "tags": article.tags,
        "video": {"url": meta.watch_url, "title": meta.title, "publishedAt": meta.published_at},
        # A string: the front end puts it into <script type="application/ld+json"> as is.
        "jsonLd": json.dumps(schemas, ensure_ascii=False),
    }
    if settings.site_url:
        fields["canonicalUrl"] = f"{settings.site_url}/{article.slug}"
    return fields


# --- HTTP API -------------------------------------------------------------------

class PayloadConflict(cms.DraftConflict):
    pass


class PayloadRejected(Exception):
    """Payload refused the post (HTTP 400), with the fields it complained about."""


def _rejected(exc: httpx.HTTPStatusError, data: dict) -> Exception:
    """A readable error for a 400 from a create or update, or the original error for anything else."""
    if exc.response.status_code != 400:
        return exc
    try:
        errors = exc.response.json().get("errors", [])
    except ValueError:
        errors = []
    fields = [(e.get("path", ""), e.get("message", "")) for error in errors
              for e in (error.get("data") or {}).get("errors", [])]
    if any(path == "slug" and "unique" in message.lower() for path, message in fields):
        # Payload's slug field is unique, e.g. after `run --force` left the earlier draft in place.
        return PayloadConflict(f"A Payload post with the slug {data.get('slug')!r} already exists. "
                               "Delete or rename that post in Payload, then push again")
    details = "; ".join(f"{path}: {message}" for path, message in fields) or "; ".join(
        error.get("message", "") for error in errors) or exc.response.text[:300]
    return PayloadRejected(f"Payload rejected the post: {details}")


class PayloadClient:
    def __init__(self, settings: Settings):
        settings.require("payload_url")
        if not is_configured(settings):
            raise ConfigError("Missing required config: PAYLOAD_API_KEY, or PAYLOAD_EMAIL and PAYLOAD_PASSWORD "
                              "(set it in .env)")
        self.url = settings.payload_url
        self.collection = settings.payload_collection
        self.media = settings.payload_media_collection
        self._settings = settings
        self._auth: str | None = None

    def _headers(self) -> dict:
        if self._auth is None:
            s = self._settings
            if s.payload_api_key:
                self._auth = f"{s.payload_auth_collection} API-Key {s.payload_api_key}"
            else:
                response = net.request("POST", f"{self.url}/api/{s.payload_auth_collection}/login",
                                       json={"email": s.payload_email, "password": s.payload_password})
                self._auth = f"JWT {response.json()['token']}"
        return {"Authorization": self._auth}

    def _save(self, method: str, path: str, data: dict) -> dict:
        try:
            response = net.request(method, f"{self.url}/api/{path}", headers=self._headers(),
                                   params={"draft": "true", "depth": 0}, json=data)
        except httpx.HTTPStatusError as exc:
            error = _rejected(exc, data)
            if error is exc:
                raise
            raise error from exc
        return response.json()["doc"]

    def create(self, data: dict) -> dict:
        return self._save("POST", self.collection, data)

    def update(self, doc_id: str, data: dict) -> dict:
        return self._save("PATCH", f"{self.collection}/{doc_id}", data)

    def get(self, doc_id: str, draft: bool) -> dict | None:
        """The post's latest draft (draft=True) or its main document; None if it was deleted."""
        try:
            response = net.request("GET", f"{self.url}/api/{self.collection}/{doc_id}", headers=self._headers(),
                                   params={"draft": "true" if draft else "false", "depth": 0})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return response.json()

    def state(self, doc_id: str) -> dict:
        """{"draft": the latest draft or None, "published": whether the post has been published}."""
        draft = self.get(doc_id, draft=True)
        if draft is None:
            return {"draft": None, "published": False}
        main = self.get(doc_id, draft=False) or {}
        return {"draft": draft, "published": main.get("_status") == "published"}

    def upload_image(self, data: bytes, filename: str, alt: str) -> str:
        response = net.request(
            "POST",
            f"{self.url}/api/{self.media}",
            headers=self._headers(),
            files={"file": (filename, data, guess_type(filename)[0] or "image/jpeg")},
            data={"_payload": json.dumps({"alt": alt})},
        )
        return response.json()["doc"]["id"]

    def editor_url(self, doc_id: str) -> str:
        return f"{self.url}/admin/collections/{self.collection}/{doc_id}"


def check_updatable(state: dict, last_updated: str | None) -> None:
    """Raise PayloadConflict unless the post is unpublished and its draft unchanged since our last push."""
    if state["published"]:
        raise PayloadConflict("The Payload post has been published, so it won't be overwritten. "
                              "Use `yt2blog run --force` to create a new draft instead")
    if state["draft"].get("updatedAt") != last_updated:
        raise PayloadConflict(
            "The Payload draft was edited after the last push, so updating it would lose those edits. "
            "Use `yt2blog run --force` to create a new draft instead"
        )


# --- stage ----------------------------------------------------------------------

def is_configured(settings: Settings) -> bool:
    return bool(settings.payload_url and (settings.payload_api_key
                                          or (settings.payload_email and settings.payload_password)))


def run(ctx: Context) -> None:
    if ctx.destination != "payload":
        log.info("Not sending this run to Payload, skipping the draft")
        return
    if not is_configured(ctx.settings):
        log.info("Payload is not configured (PAYLOAD_API_KEY, or PAYLOAD_EMAIL and PAYLOAD_PASSWORD, is empty), "
                 "skipping the draft")
        return
    mapping = cms.parse_field_map(ctx.settings.payload_fields, "PAYLOAD_FIELDS")
    payload = PayloadClient(ctx.settings)
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    schemas = ctx.load_json(render.JSONLD_OUTPUT)
    posts = ctx.load_json(generate.POSTS_OUTPUT) if ctx.path(generate.POSTS_OUTPUT).exists() else []
    content = build_content(article, meta, render.resolve_links(article, posts))
    fields = build_fields(article, meta, schemas, content, ctx.settings)

    doc_id = (ctx.jobs.get(ctx.video_id) or {}).get("payload_document_id")
    if doc_id:
        state = payload.state(doc_id)
        if state["draft"]:
            last_updated = ctx.load_json(OUTPUT).get("updated_at") if ctx.path(OUTPUT).exists() else None
            check_updatable(state, last_updated)
            payload.update(doc_id, cms.apply_field_map(fields, mapping))
            _save(ctx, payload, doc_id)
            log.info("Payload draft updated: %s", doc_id)
            return
        log.warning("Payload post %s no longer exists, creating a new draft", doc_id)

    document = {**fields, "_status": "draft"}
    if meta.thumbnail_url:
        image = net.request("GET", meta.thumbnail_url).content
        filename = f"{ctx.video_id}-{urlparse(meta.thumbnail_url).path.rsplit('/', 1)[-1]}"
        image_id = payload.upload_image(image, filename, meta.title)
        document["heroImage"] = image_id
        document["meta"] = {**document["meta"], "image": image_id}
    doc_id = str(payload.create(cms.apply_field_map(document, mapping))["id"])
    _save(ctx, payload, doc_id)
    log.info("Payload draft created: %s", doc_id)


def _save(ctx: Context, payload: PayloadClient, doc_id: str) -> None:
    # updated_at is what the next run compares against to spot edits made in the admin panel.
    draft = payload.get(doc_id, draft=True) or {}
    editor_url = payload.editor_url(doc_id)
    ctx.save_json(OUTPUT, {"id": doc_id, "editor_url": editor_url, "updated_at": draft.get("updatedAt")})
    ctx.jobs.upsert(ctx.video_id, payload_document_id=doc_id, payload_editor_url=editor_url)
