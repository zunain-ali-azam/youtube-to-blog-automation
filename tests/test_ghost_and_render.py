import json

import jwt

from explainer.config import Settings
from explainer.models import Article, CodeBlock, FAQItem, Section, VideoMetadata
from explainer.stages.render import build_html, build_jsonld, build_markdown, html_to_markdown, jsonld_script
from explainer.stages.ghost import admin_token, build_post, build_tags

SECRET_HEX = "a1" * 32
KEY = f"6489c5f2e4b0a1b2c3d4e5f6:{SECRET_HEX}"


def test_admin_token():
    token = admin_token(KEY, now=1_700_000_000)
    assert jwt.get_unverified_header(token) == {"alg": "HS256", "kid": "6489c5f2e4b0a1b2c3d4e5f6", "typ": "JWT"}
    payload = jwt.decode(
        token, bytes.fromhex(SECRET_HEX), algorithms=["HS256"], audience="/admin/", options={"verify_exp": False}
    )
    assert payload == {"iat": 1_700_000_000, "exp": 1_700_000_300, "aud": "/admin/"}


def test_admin_token_rejects_bad_key():
    import pytest
    with pytest.raises(ValueError):
        admin_token("no-colon-here")


def _meta() -> VideoMetadata:
    return VideoMetadata(
        video_id="dQw4w9WgXcQ", title="Signals & <You>", description="d", published_at="2025-03-01T14:00:00Z",
        duration_iso="PT10M", duration_seconds=600, channel_title="Code with Ahsan", channel_id="UC123",
        thumbnail_url="https://i.ytimg.com/max.jpg",
    )


def _article() -> Article:
    return Article(
        title="Angular Signals explained", slug="angular-signals", meta_title="Angular Signals",
        meta_description="m" * 130, excerpt="e", intro_html="<p>Intro</p>",
        learning_points=["Create a signal"],
        sections=[
            Section(heading="Creating signals", body_html="<p>Body</p>", start_seconds=272,
                    code_blocks=[CodeBlock(language="TypeScript", code="const a = signal(1) < 2 && x;")]),
            Section(heading="Effects", body_html="<p>More</p>", start_seconds=3735),
        ],
        key_takeaways=["Signals are sync"],
        faq=[FAQItem(question="What is a signal?", answer_html="<p>A value.</p>")],
        tags=["Angular", "Video-Explainer", "Signals"],
    )


def test_build_html_order_and_escaping():
    html = build_html(_article(), _meta())
    order = ["<p>Intro</p>", "What you'll learn", "kg-card-begin: html", "Creating signals", "Effects",
             "Key takeaways", "Frequently asked questions", "Code with Ahsan"]
    positions = [html.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "https://www.youtube.com/embed/dQw4w9WgXcQ" in html
    assert '<a href="https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=272s">Watch this part (4:32)</a>' in html
    assert "Watch this part (1:02:15)" in html
    assert '<code class="language-typescript">const a = signal(1) &lt; 2 &amp;&amp; x;</code>' in html
    assert 'title="Signals &amp; &lt;You&gt;"' in html


def test_jsonld():
    data = build_jsonld(_article(), _meta())
    assert data["@type"] == "VideoObject"
    assert data["duration"] == "PT10M"
    assert data["embedUrl"] == "https://www.youtube.com/embed/dQw4w9WgXcQ"
    script = jsonld_script({"name": "</script><b>"})
    assert "</script><b>" not in script.removesuffix("</script>")
    assert json.loads(script.removeprefix('<script type="application/ld+json">').removesuffix("</script>").replace("<\\/", "</"))


def test_build_tags_primary_first_and_deduped():
    assert build_tags("video-explainer", ["Angular", "Video-Explainer", " ", "angular", "Signals"]) == [
        "video-explainer", "Angular", "Signals",
    ]


def test_build_post():
    settings = Settings(ghost_author_email="ahsan@example.com")
    post = build_post(_article(), "<p>x</p>", "<script></script>", settings, "https://ghost/img.jpg")
    assert post["status"] == "draft"
    assert post["tags"][0] == "video-explainer"
    assert post["authors"] == ["ahsan@example.com"]
    assert post["feature_image"] == "https://ghost/img.jpg"
    assert post["codeinjection_head"] == "<script></script>"
    assert post["og_title"] == post["twitter_title"] == "Angular Signals"
    assert post["og_description"] == post["meta_description"]
    assert "canonical_url" not in post
    with_site = build_post(_article(), "", "", Settings(site_url="https://iomechs.com/blog/"), None)
    assert with_site["canonical_url"] == "https://iomechs.com/blog/angular-signals"


def test_faq_schema():
    from explainer.stages.render import build_schemas
    schemas = build_schemas(_article(), _meta())
    assert [s["@type"] for s in schemas] == ["VideoObject", "FAQPage"]
    assert schemas[1]["mainEntity"][0] == {
        "@type": "Question", "name": "What is a signal?",
        "acceptedAnswer": {"@type": "Answer", "text": "A value."},
    }
    assert build_schemas(_article().model_copy(update={"faq": []}), _meta())[0]["@type"] == "VideoObject"
    assert len(build_schemas(_article().model_copy(update={"faq": []}), _meta())) == 1


def test_html_to_markdown():
    html = (
        "<p>Use <code>signal()</code> for <strong>state</strong>, see <a href=\"https://angular.dev\">the docs</a>.</p>\n"
        "<ul>\n<li>One</li>\n<li><em>Two</em></li>\n</ul>\n<ol><li>First</li><li>Second</li></ol>"
    )
    assert html_to_markdown(html) == (
        "Use `signal()` for **state**, see [the docs](https://angular.dev).\n\n"
        "- One\n- *Two*\n\n"
        "1. First\n2. Second"
    )


def test_build_markdown():
    article, meta = _article(), _meta()
    md = build_markdown(article, meta, [build_jsonld(article, meta)])
    front, body = md.removeprefix("---\n").split("\n---\n", 1)
    assert 'title: "Angular Signals explained"' in front
    assert 'tags: ["Angular", "Video-Explainer", "Signals"]' in front
    assert '"@type": "VideoObject"' in front
    order = ["# Angular Signals explained", "## What you'll learn", "<iframe", "## Creating signals",
             "## Effects", "## Key takeaways", "### What is a signal?", "Code with Ahsan"]
    positions = [body.index(marker) for marker in order]
    assert positions == sorted(positions)
    assert "[▶ Watch this part (4:32)](https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=272s)" in body
    assert "```typescript\nconst a = signal(1) < 2 && x;\n```" in body
    assert "kg-card" not in body


def test_code_fence_longer_than_backticks_in_code():
    from explainer.stages.render import code_markdown
    block = CodeBlock(language="markdown", code="```js\nx\n```")
    assert code_markdown(block).startswith("````markdown\n")


def test_ghost_skipped_when_not_configured(tmp_path):
    from explainer.context import Context
    from explainer.jobs import JobStore
    from explainer.stages import ghost

    settings = Settings(data_dir=tmp_path)
    ctx = Context("dQw4w9WgXcQ", settings, JobStore(tmp_path / "jobs.db"), "captions")
    ghost.run(ctx)
    assert not ctx.path(ghost.OUTPUT).exists()


def test_markdown_escapes_plain_text():
    article, meta = _article(), _meta()
    md = build_markdown(article, meta, [build_jsonld(article, meta)])
    assert r"[Signals & \<You\>](https://www.youtube.com/watch?v=dQw4w9WgXcQ)" in md


def test_list_items_keep_inline_code():
    article = _article().model_copy(update={"learning_points": ["Call <code>signal()</code> to create one"]})
    assert "<li>Call <code>signal()</code> to create one</li>" in build_html(article, _meta())
    assert "- Call `signal()` to create one" in build_markdown(article, _meta(), {})


def _render_ctx(tmp_path):
    from explainer.context import Context
    from explainer.jobs import JobStore
    from explainer.stages import generate, metadata

    settings = Settings(data_dir=tmp_path / "data", blogs_dir=tmp_path / "blogs")
    ctx = Context("dQw4w9WgXcQ", settings, JobStore(tmp_path / "jobs.db"), "captions")
    ctx.save_model(generate.OUTPUT, _article())
    ctx.save_model(metadata.OUTPUT, _meta())
    return ctx


def test_render_saves_blog_to_blogs_dir(tmp_path):
    from explainer.stages import render

    ctx = _render_ctx(tmp_path)
    render.run(ctx)
    blog = tmp_path / "blogs" / "angular-signals.md"
    assert blog.read_text(encoding="utf-8") == ctx.path(render.OUTPUT).read_text(encoding="utf-8")


def test_ghost_skipped_when_run_says_no(tmp_path):
    from explainer.stages import ghost

    ctx = _render_ctx(tmp_path)
    ctx.settings = ctx.settings.model_copy(update={"ghost_admin_api_key": KEY})
    ctx.ghost = False
    ghost.run(ctx)
    assert not ctx.path(ghost.OUTPUT).exists()


def test_push_finds_blog_by_slug_before_video_id(tmp_path):
    from explainer.cli import _find_video

    ctx = _render_ctx(tmp_path)
    # An 11-character slug would also pass as a video ID; the blog must win.
    ctx.save_model("article.json", _article().model_copy(update={"slug": "angular-sig"}))
    assert _find_video("angular-sig", ctx.settings) == "dQw4w9WgXcQ"
    assert _find_video("blogs/angular-sig.md", ctx.settings) == "dQw4w9WgXcQ"
    assert _find_video("https://youtu.be/abcdefghijk", ctx.settings) == "abcdefghijk"
