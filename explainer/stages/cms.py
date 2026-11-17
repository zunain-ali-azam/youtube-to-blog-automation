"""What the CMS destination stages share: the order of the post body, and field renames.

Each CMS stores rich text in its own format (Portable Text for Sanity, Lexical JSON for
Payload), so `body_parts` gives the body as a list of parts each stage converts:
("html", fragment), ("youtube", url) or ("code", language, code).
"""

from html import escape

from explainer.config import ConfigError
from explainer.models import Article, VideoMetadata
from explainer.stages import render


def _list_html(items: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"


def body_parts(article: Article, meta: VideoMetadata, links: list[tuple[str, str]] = ()) -> list[tuple]:
    """The article body in the same order as render.build_html, with the video and code as their own parts."""
    parts: list[tuple] = [("html", article.intro_html)]
    if article.learning_points:
        parts.append(("html", "<h2>What you'll learn</h2>" + _list_html(article.learning_points)))
    parts.append(("youtube", meta.watch_url))

    for section in article.sections:
        parts.append(("html", f"<h2>{escape(section.heading)}</h2>" + render.watch_link(meta, section.start_seconds)))
        parts.append(("html", section.body_html))
        parts += [("code", block.language.lower().strip(), block.code) for block in section.code_blocks]

    if article.key_takeaways:
        parts.append(("html", "<h2>Key takeaways</h2>" + _list_html(article.key_takeaways)))
    if article.faq:
        parts.append(("html", "<h2>Frequently asked questions</h2>"))
        parts += [("html", f"<h3>{escape(item.question)}</h3>" + item.answer_html) for item in article.faq]
    if links:
        parts.append(("html", render.related_html(links)))
    parts.append(("html", render.credit_html(meta)))
    return parts


class DraftConflict(Exception):
    """A push refused because it would overwrite the CMS post: it was published or edited there.
    Expected, so the pipeline logs only its message, without a traceback."""


def parse_field_map(spec: str, env_name: str) -> dict[str, str | None]:
    """A *_FIELDS setting ("content=body,seo=-") -> {"content": "body", "seo": None}."""
    mapping = {}
    for pair in filter(None, (part.strip() for part in spec.split(","))):
        name, sep, target = (p.strip() for p in pair.partition("="))
        if not sep or not name or not target:
            raise ConfigError(f"{env_name} entry {pair!r} should look like field=newName or field=-")
        mapping[name] = None if target == "-" else target
    return mapping


def apply_field_map(fields: dict, mapping: dict[str, str | None]) -> dict:
    result = {}
    for name, value in fields.items():
        target = mapping.get(name, name)
        if target is not None:
            result[target] = value
    return result
