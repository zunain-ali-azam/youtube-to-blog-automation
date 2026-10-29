"""Stage 4: render the article -> article.html, jsonld.json, article.md

`article.md` is the main deliverable: a complete blog post with the SEO fields in
YAML front matter, ready to read, edit or paste into any CMS. `article.html` and
`jsonld.json` are what the Ghost stage posts. A copy of `article.md` is kept as
generated_blogs/<slug>.md, the folder where every finished blog ends up. `jsonld.json` is a list: the
VideoObject, plus an FAQPage when the article has FAQs.

The article's body fields are a small HTML subset (see prompts/generate_article.md),
so `html_to_markdown` only needs to handle those tags.

Internal links are rendered from ghost_posts.json (saved by the generate stage), and
only for slugs that are really there, so a made-up slug can't become a broken link.
"""

import json
import logging
import re
from html import escape, unescape
from html.parser import HTMLParser
from pathlib import Path

from explainer.config import Settings
from explainer.context import Context
from explainer.models import Article, CodeBlock, VideoMetadata
from explainer.stages import generate, metadata as metadata_stage
from explainer.timestamps import format_timestamp

log = logging.getLogger(__name__)

OUTPUT = "article.md"
HTML_OUTPUT = "article.html"
JSONLD_OUTPUT = "jsonld.json"


def _iframe(meta: VideoMetadata) -> str:
    return (
        '<div style="position:relative;padding-bottom:56.25%;height:0;overflow:hidden;">'
        f'<iframe src="{meta.embed_url}" title="{escape(meta.title)}" '
        'style="position:absolute;top:0;left:0;width:100%;height:100%;border:0;" '
        'allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" '
        'referrerpolicy="strict-origin-when-cross-origin" allowfullscreen></iframe></div>'
    )


def embed_html(meta: VideoMetadata) -> str:
    # The kg-card markers make Ghost keep this block as an HTML card.
    return f"<!--kg-card-begin: html-->\n{_iframe(meta)}\n<!--kg-card-end: html-->"


def watch_url(meta: VideoMetadata, seconds: int) -> str:
    return f"{meta.watch_url}&t={seconds}s"


# --- HTML -----------------------------------------------------------------------

def code_html(block: CodeBlock) -> str:
    language = escape(block.language.lower().strip() or "plaintext")
    return f'<pre><code class="language-{language}">{escape(block.code)}</code></pre>'


def watch_link(meta: VideoMetadata, seconds: int) -> str:
    return f'<p><a href="{watch_url(meta, seconds)}">Watch this part ({format_timestamp(seconds)})</a></p>'


def _list(items: list[str]) -> str:
    # Items are inline HTML (the same subset as the body fields), so they aren't escaped.
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def resolve_links(article: Article, posts: list[dict]) -> list[tuple[str, str]]:
    """(anchor text, url) for each internal link whose slug is a published post."""
    urls = {post["slug"]: post["url"] for post in posts}
    links = []
    for link in article.internal_links:
        if link.slug in urls:
            links.append((link.anchor_text or link.title, urls[link.slug]))
        else:
            log.warning("Dropping internal link to unknown slug %r", link.slug)
    return links


def build_html(article: Article, meta: VideoMetadata, links: list[tuple[str, str]] = ()) -> str:
    parts = [article.intro_html]
    if article.learning_points:
        parts += ["<h2>What you'll learn</h2>", _list(article.learning_points)]
    parts.append(embed_html(meta))

    for section in article.sections:
        parts.append(f"<h2>{escape(section.heading)}</h2>")
        parts.append(watch_link(meta, section.start_seconds))
        parts.append(section.body_html)
        parts += [code_html(block) for block in section.code_blocks]

    if article.key_takeaways:
        parts += ["<h2>Key takeaways</h2>", _list(article.key_takeaways)]
    if article.faq:
        parts.append("<h2>Frequently asked questions</h2>")
        for item in article.faq:
            parts += [f"<h3>{escape(item.question)}</h3>", item.answer_html]
    if links:
        parts += ["<h2>Related articles</h2>",
                  _list([f'<a href="{escape(url)}">{escape(text)}</a>' for text, url in links])]

    parts.append(
        "<hr><p><em>This article is based on the video "
        f'<a href="{meta.watch_url}">{escape(meta.title)}</a> by '
        f'<a href="{meta.channel_url}">{escape(meta.channel_title)}</a>.</em></p>'
    )
    return "\n".join(parts)


def build_jsonld(article: Article, meta: VideoMetadata) -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "VideoObject",
        "name": meta.title,
        "description": article.meta_description or meta.description[:300],
        "thumbnailUrl": meta.thumbnail_url,
        "uploadDate": meta.published_at,
        "duration": meta.duration_iso,
        "embedUrl": meta.embed_url,
        "contentUrl": meta.watch_url,
    }


def build_faq_jsonld(article: Article) -> dict | None:
    if not article.faq:
        return None
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": item.question,
             "acceptedAnswer": {"@type": "Answer", "text": html_text(item.answer_html)}}
            for item in article.faq
        ],
    }


def build_schemas(article: Article, meta: VideoMetadata) -> list[dict]:
    faq = build_faq_jsonld(article)
    return [build_jsonld(article, meta), *([faq] if faq else [])]


def html_text(html: str) -> str:
    """Plain text of an HTML fragment, whitespace collapsed."""
    return " ".join(unescape(re.sub(r"<[^>]+>", " ", html)).split())


def jsonld_script(data: dict) -> str:
    # Escape "</" so the JSON can't close the script tag early.
    body = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return f'<script type="application/ld+json">{body}</script>'


# --- Markdown -------------------------------------------------------------------

class _MarkdownConverter(HTMLParser):
    """Converts the article's HTML subset (p, ul, ol, li, strong, b, em, i, code, a, br)
    to Markdown. Unknown tags are dropped and their text kept."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list] = []  # one [kind, counter] per open list
        self.hrefs: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        if tag in ("p", "ul", "ol") and not self.lists:
            self.out.append("\n\n")
        if tag in ("ul", "ol"):
            self.lists.append([tag, 0])
        elif tag == "li" and self.lists:
            current = self.lists[-1]
            current[1] += 1
            marker = f"{current[1]}." if current[0] == "ol" else "-"
            self.out.append("\n" + "  " * (len(self.lists) - 1) + marker + " ")
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "code":
            self.out.append("`")
        elif tag == "a":
            self.hrefs.append(dict(attrs).get("href"))
            self.out.append("[")
        elif tag == "br":
            self.out.append("  \n")

    def handle_endtag(self, tag):
        if tag in ("ul", "ol") and self.lists:
            self.lists.pop()
        elif tag in ("strong", "b"):
            self.out.append("**")
        elif tag in ("em", "i"):
            self.out.append("*")
        elif tag == "code":
            self.out.append("`")
        elif tag == "a" and self.hrefs:
            href = self.hrefs.pop()
            self.out.append(f"]({href})" if href else "]")

    def handle_data(self, data):
        data = re.sub(r"\s+", " ", data)
        if not self.out or self.out[-1].endswith(("\n", " ")):
            data = data.lstrip()
        if data:
            self.out.append(data)


def html_to_markdown(html: str) -> str:
    converter = _MarkdownConverter()
    converter.feed(html)
    converter.close()
    text = "".join(converter.out)
    lines = [line.rstrip() if not line.endswith("  ") else line for line in text.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def md_text(text: str) -> str:
    """Escape plain text so Markdown doesn't read it as a tag, link or emphasis."""
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)


def _md_list(items: list[str]) -> str:
    return "\n".join(f"- {html_to_markdown(item)}" for item in items)


def code_markdown(block: CodeBlock) -> str:
    language = block.language.lower().strip()
    # Use a fence longer than any run of backticks inside the code.
    longest = max((len(run) for run in re.findall(r"`+", block.code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{block.code.rstrip()}\n{fence}"


def front_matter(article: Article, meta: VideoMetadata, jsonld: list[dict]) -> str:
    # JSON values are valid YAML, which keeps quoting and escaping simple.
    fields = {
        "title": article.title,
        "slug": article.slug,
        "meta_title": article.meta_title,
        "meta_description": article.meta_description,
        "excerpt": article.excerpt,
        "tags": article.tags,
        "feature_image": meta.thumbnail_url,
        "video_url": meta.watch_url,
        "video_title": meta.title,
        "video_published_at": meta.published_at,
        "channel": meta.channel_title,
        "jsonld": jsonld,
    }
    lines = [f"{key}: {json.dumps(value, ensure_ascii=False)}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---"


def build_markdown(article: Article, meta: VideoMetadata, jsonld: list[dict], links: list[tuple[str, str]] = ()) -> str:
    parts = [front_matter(article, meta, jsonld), f"# {md_text(article.title)}", html_to_markdown(article.intro_html)]
    if article.learning_points:
        parts += ["## What you'll learn", _md_list(article.learning_points)]
    parts.append(_iframe(meta))

    for section in article.sections:
        parts.append(f"## {md_text(section.heading)}")
        parts.append(
            f"[▶ Watch this part ({format_timestamp(section.start_seconds)})]"
            f"({watch_url(meta, section.start_seconds)})"
        )
        parts.append(html_to_markdown(section.body_html))
        parts += [code_markdown(block) for block in section.code_blocks]

    if article.key_takeaways:
        parts += ["## Key takeaways", _md_list(article.key_takeaways)]
    if article.faq:
        parts.append("## Frequently asked questions")
        for item in article.faq:
            parts += [f"### {md_text(item.question)}", html_to_markdown(item.answer_html)]
    if links:
        parts += ["## Related articles", "\n".join(f"- [{md_text(text)}]({url})" for text, url in links)]

    parts += [
        "---",
        f"*This article is based on the video [{md_text(meta.title)}]({meta.watch_url}) "
        f"by [{md_text(meta.channel_title)}]({meta.channel_url}).*",
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def run(ctx: Context) -> None:
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    jsonld = build_schemas(article, meta)
    posts = ctx.load_json(generate.POSTS_OUTPUT) if ctx.path(generate.POSTS_OUTPUT).exists() else []
    links = resolve_links(article, posts)
    ctx.path(HTML_OUTPUT).write_text(build_html(article, meta, links), encoding="utf-8")
    ctx.save_json(JSONLD_OUTPUT, jsonld)
    markdown = build_markdown(article, meta, jsonld, links)
    blog = blog_path(ctx.settings, article.slug)
    blog.parent.mkdir(parents=True, exist_ok=True)
    blog.write_text(markdown, encoding="utf-8")
    # Written last: it is the file the pipeline checks to skip this stage.
    ctx.path(OUTPUT).write_text(markdown, encoding="utf-8")
    log.info("Article written: %s", blog)


def blog_path(settings: Settings, slug: str) -> Path:
    """Where the finished blog is kept: generated_blogs/<slug>.md."""
    return settings.blogs_dir / f"{slug}.md"
