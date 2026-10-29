"""Stage 3b: quality checks on the article -> checks.json

Every check produces warnings, never a failure: the job always continues and the
warnings are printed at the end of `yt2blog run` for the reviewer.

Checks: length, SEO field lengths, missing parts, passages copied from the
transcript, code still needing verification, timestamps past the end of the video,
HTML outside the allowed subset, internal links to unknown posts, and broken links.
"""

import logging
import re
from html import unescape
from html.parser import HTMLParser

import httpx

from explainer.context import Context
from explainer.models import Article, Transcript, VideoMetadata
from explainer.stages import generate, metadata as metadata_stage, transcript as transcript_stage
from explainer.stages.render import html_text
from explainer.timestamps import format_timestamp

log = logging.getLogger(__name__)

OUTPUT = "checks.json"

MIN_WORDS, MAX_WORDS = 600, 3000
MAX_META_TITLE = 60
MIN_META_DESCRIPTION, MAX_META_DESCRIPTION = 120, 155
MAX_EXCERPT = 300
COPY_WORDS = 25  # a run of this many words found verbatim in the transcript counts as copied

# The subset prompts/generate_article.md allows. List items are inline only.
BLOCK_TAGS = {"p", "ul", "ol", "li"}
INLINE_TAGS = {"strong", "b", "em", "i", "code", "a", "br"}
VOID_TAGS = {"br"}

LINK_TIMEOUT = 10
# Some sites refuse HEAD or bots; these statuses get a GET before counting as broken.
_RETRY_WITH_GET = {403, 405, 501}
_USER_AGENT = "Mozilla/5.0 (compatible; explainer-link-check)"


def warning(check: str, message: str) -> dict:
    return {"check": check, "message": message}


# --- Text helpers ----------------------------------------------------------------

def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", text.lower())


def html_fields(article: Article) -> list[tuple[str, str, bool]]:
    """(label, html, inline_only) for every HTML field of the article."""
    fields = [("intro", article.intro_html, False)]
    fields += [(f"learning point {i + 1}", item, True) for i, item in enumerate(article.learning_points)]
    fields += [(f"section {section.heading!r}", section.body_html, False) for section in article.sections]
    fields += [(f"takeaway {i + 1}", item, True) for i, item in enumerate(article.key_takeaways)]
    fields += [(f"FAQ {item.question!r}", item.answer_html, False) for item in article.faq]
    return fields


def prose(article: Article) -> list[str]:
    """The article's reader-facing text, one string per field, without code blocks."""
    texts = [html_text(html) for _, html, _ in html_fields(article)]
    return texts + [section.heading for section in article.sections] + [item.question for item in article.faq]


# --- Checks ----------------------------------------------------------------------

def check_length(article: Article) -> list[dict]:
    count = sum(len(words(text)) for text in prose(article))
    if count < MIN_WORDS:
        return [warning("word_count", f"{count} words, below the {MIN_WORDS} minimum")]
    if count > MAX_WORDS:
        return [warning("word_count", f"{count} words, above the {MAX_WORDS} maximum")]
    return []


def check_seo(article: Article) -> list[dict]:
    out = []
    if len(article.meta_title) > MAX_META_TITLE:
        out.append(warning("meta_title", f"Meta title is {len(article.meta_title)} characters (max {MAX_META_TITLE})"))
    length = len(article.meta_description)
    if not MIN_META_DESCRIPTION <= length <= MAX_META_DESCRIPTION:
        out.append(warning("meta_description", f"Meta description is {length} characters "
                                               f"(want {MIN_META_DESCRIPTION} to {MAX_META_DESCRIPTION})"))
    if len(article.excerpt) > MAX_EXCERPT:
        out.append(warning("excerpt", f"Excerpt is {len(article.excerpt)} characters (max {MAX_EXCERPT}); "
                                      "Ghost will cut it"))
    return out


def check_structure(article: Article) -> list[dict]:
    out = []
    if not html_text(article.intro_html).strip():
        out.append(warning("structure", "The intro is empty"))
    if not article.learning_points:
        out.append(warning("structure", "No \"What you'll learn\" points"))
    if not article.key_takeaways:
        out.append(warning("structure", "No key takeaways"))
    if len(article.sections) < 2:
        out.append(warning("structure", f"Only {len(article.sections)} section(s); expected at least 2"))
    return out


def copied_passages(texts: list[str], transcript_text: str, n: int = COPY_WORDS) -> list[str]:
    """Passages of n+ words that appear word for word in the transcript (case and punctuation ignored)."""
    source = words(transcript_text)
    ngrams = {tuple(source[i:i + n]) for i in range(len(source) - n + 1)}
    passages = []
    for text in texts:
        tokens = words(text)
        i = 0
        while i <= len(tokens) - n:
            if tuple(tokens[i:i + n]) not in ngrams:
                i += 1
                continue
            end = i + n
            while end < len(tokens) and tuple(tokens[end - n + 1:end + 1]) in ngrams:
                end += 1
            passages.append(" ".join(tokens[i:end]))
            i = end
    return passages


def check_copying(article: Article, transcript: Transcript) -> list[dict]:
    transcript_text = " ".join(segment.text for segment in transcript.segments)
    return [
        warning("copied_text", f"{len(passage.split())} words match the transcript verbatim: \"{passage}\"")
        for passage in copied_passages(prose(article), transcript_text)
    ]


def check_code(article: Article) -> list[dict]:
    flagged = [
        f"{section.heading!r} ({block.language})"
        for section in article.sections
        for block in section.code_blocks
        if block.needs_verification
    ]
    if not flagged:
        return []
    return [warning("code_unverified", f"{len(flagged)} code block(s) transcribed from the video, not matched to "
                                       f"the repo; check them against the video: {', '.join(flagged)}")]


def check_timestamps(article: Article, meta: VideoMetadata) -> list[dict]:
    return [
        warning("timestamp", f"Section {section.heading!r} starts at {format_timestamp(section.start_seconds)}, "
                             f"after the video ends ({format_timestamp(meta.duration_seconds)})")
        for section in article.sections
        if not 0 <= section.start_seconds < meta.duration_seconds
    ]


class _TagChecker(HTMLParser):
    def __init__(self, allowed: set[str]):
        super().__init__(convert_charrefs=True)
        self.allowed = allowed
        self.problems: list[str] = []
        self.stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in self.allowed:
            self.problems.append(f"<{tag}> isn't allowed here")
        elif tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        # A disallowed tag was already reported when it opened.
        if tag in VOID_TAGS or tag not in self.allowed:
            return
        if tag in self.stack:
            # Everything opened after this tag was left unclosed.
            while self.stack[-1] != tag:
                self.problems.append(f"<{self.stack.pop()}> isn't closed")
            self.stack.pop()
        else:
            self.problems.append(f"</{tag}> has no opening tag")

    def close(self):
        super().close()
        self.problems += [f"<{tag}> isn't closed" for tag in self.stack]


def html_problems(html: str, inline_only: bool) -> list[str]:
    checker = _TagChecker(INLINE_TAGS if inline_only else INLINE_TAGS | BLOCK_TAGS)
    checker.feed(html)
    checker.close()
    return checker.problems


def check_html(article: Article) -> list[dict]:
    out = []
    for label, html, inline_only in html_fields(article):
        for problem in dict.fromkeys(html_problems(html, inline_only)):
            out.append(warning("html", f"{label}: {problem}"))
    return out


def check_internal_links(article: Article, posts: list[dict]) -> list[dict]:
    slugs = {post["slug"] for post in posts}
    return [
        warning("internal_link", f"Internal link to unknown post {link.slug!r} was left out")
        for link in article.internal_links
        if link.slug not in slugs
    ]


def external_links(article: Article) -> list[str]:
    links = []
    for _, html, _ in html_fields(article):
        for href in re.findall(r'<a\s[^>]*href="([^"]+)"', html):
            href = unescape(href)
            if href.startswith(("http://", "https://")) and href not in links:
                links.append(href)
    return links


def link_status(client: httpx.Client, url: str) -> str | None:
    """None if the link works, otherwise a short reason."""
    try:
        response = client.head(url)
        if response.status_code in _RETRY_WITH_GET:
            response = client.get(url)
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return f"HTTP {response.status_code}" if response.status_code >= 400 else None


def check_links(article: Article) -> list[dict]:
    out = []
    headers = {"User-Agent": _USER_AGENT}
    with httpx.Client(timeout=LINK_TIMEOUT, follow_redirects=True, headers=headers) as client:
        for url in external_links(article):
            reason = link_status(client, url)
            if reason:
                out.append(warning("broken_link", f"{url} ({reason})"))
    return out


def run_checks(article: Article, meta: VideoMetadata, transcript: Transcript, posts: list[dict]) -> list[dict]:
    return [
        *check_structure(article),
        *check_length(article),
        *check_seo(article),
        *check_copying(article, transcript),
        *check_code(article),
        *check_timestamps(article, meta),
        *check_html(article),
        *check_internal_links(article, posts),
        *check_links(article),
    ]


def run(ctx: Context) -> None:
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    transcript = ctx.load_model(transcript_stage.OUTPUT, Transcript)
    posts = ctx.load_json(generate.POSTS_OUTPUT) if ctx.path(generate.POSTS_OUTPUT).exists() else []
    warnings = run_checks(article, meta, transcript, posts)
    ctx.save_json(OUTPUT, warnings)
    log.info("Checks: %d warning(s)", len(warnings))
