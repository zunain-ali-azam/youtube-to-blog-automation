import httpx
import pytest

from explainer.config import Settings
from explainer.context import Context
from explainer.jobs import JobStore
from explainer.models import Article, CodeBlock, InternalLink, Section, Segment, Transcript, VideoMetadata
from explainer.pipeline import StageFailed, finish, get_stage, run_stage
from explainer.stages import checks, code_fetch, generate, ghost, metadata as metadata_stage, render

VID = "dQw4w9WgXcQ"


def _meta(**update) -> VideoMetadata:
    return VideoMetadata(
        video_id=VID, title="t", description="d", published_at="2025-03-01T14:00:00Z", duration_iso="PT10M",
        duration_seconds=600, channel_title="Code with Ahsan", channel_id="UC123", thumbnail_url="",
        github_urls=["https://github.com/octo/app"], **update,
    )


def _article(**update) -> Article:
    fields = dict(
        title="T", slug="t", meta_title="Angular Signals", meta_description="m" * 130, excerpt="e",
        intro_html="<p>Intro</p>", learning_points=["Use <code>signal()</code>"],
        sections=[
            Section(heading="One", body_html="<p>Body</p>", start_seconds=10,
                    code_blocks=[CodeBlock(language="ts", code="const a = signal(1)")]),
            Section(heading="Two", body_html="<p>More</p>", start_seconds=20,
                    code_blocks=[CodeBlock(language="bash", code="npm i")]),
        ],
        key_takeaways=["Signals are sync"], tags=["Angular"],
    )
    fields.update(update)
    return Article(**fields)


@pytest.fixture
def ctx(tmp_path) -> Context:
    return Context(VID, Settings(data_dir=tmp_path), JobStore(tmp_path / "jobs.db"), "captions")


# --- Checks ----------------------------------------------------------------------

def test_copied_passages_finds_long_verbatim_runs_only():
    spoken = " ".join(f"w{i}" for i in range(40))
    copied = "Intro. " + " ".join(f"W{i}," for i in range(5, 35)) + " then something else"
    short = " ".join(f"w{i}" for i in range(10))
    assert checks.copied_passages([copied, short], spoken, n=25) == [" ".join(f"w{i}" for i in range(5, 35))]


def test_html_problems():
    assert checks.html_problems("<p>Use <code>x</code> and <a href='u'>y</a><br></p>", inline_only=False) == []
    assert checks.html_problems("<p>Use <code>x</p>", inline_only=False) == ["<code> isn't closed"]
    assert checks.html_problems("<p>block</p>", inline_only=True) == ["<p> isn't allowed here"]
    assert checks.html_problems("<h2>x</h2>", inline_only=False) == ["<h2> isn't allowed here"]
    assert checks.html_problems("x</em>", inline_only=True) == ["</em> has no opening tag"]


def test_seo_structure_and_length_warnings():
    article = _article(meta_title="x" * 61, meta_description="short", sections=[], key_takeaways=[])
    found = {w["check"] for w in checks.check_seo(article) + checks.check_structure(article) + checks.check_length(article)}
    assert found == {"meta_title", "meta_description", "structure", "word_count"}


def test_code_timestamp_and_internal_link_warnings():
    article = _article(internal_links=[InternalLink(title="x", slug="missing", anchor_text="x")])
    article.sections[1].start_seconds = 700
    assert len(checks.check_code(article)) == 1
    assert "after the video ends" in checks.check_timestamps(article, _meta())[0]["message"]
    assert checks.check_internal_links(article, [{"slug": "other"}])[0]["check"] == "internal_link"


def test_link_status_retries_head_refusals_with_get():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/gone":
            return httpx.Response(404)
        return httpx.Response(405 if request.method == "HEAD" else 200)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert checks.link_status(client, "https://example.com/ok") is None
        assert checks.link_status(client, "https://example.com/gone") == "HTTP 404"


def test_external_links_from_html():
    article = _article(intro_html='<p><a href="https://a.dev/x?a=1&amp;b=2">a</a> <a href="/local">b</a></p>')
    assert checks.external_links(article) == ["https://a.dev/x?a=1&b=2"]


# --- Internal links in render ------------------------------------------------------

def test_related_articles_only_for_known_slugs():
    article = _article(internal_links=[
        InternalLink(title="Signals", slug="signals", anchor_text="Angular Signals guide"),
        InternalLink(title="Made up", slug="made-up", anchor_text="Nope"),
    ])
    links = render.resolve_links(article, [{"slug": "signals", "url": "https://blog.dev/signals/"}])
    assert links == [("Angular Signals guide", "https://blog.dev/signals/")]
    html = render.build_html(article, _meta(), links)
    assert '<h2>Related articles</h2>\n<ul><li><a href="https://blog.dev/signals/">Angular Signals guide</a></li></ul>' in html
    md = render.build_markdown(article, _meta(), {}, links)
    assert "## Related articles\n\n- [Angular Signals guide](https://blog.dev/signals/)" in md


# --- Code from repo ------------------------------------------------------------------

def test_repo_helpers():
    assert code_fetch.repo_name("https://github.com/octo/app") == "octo/app"
    assert code_fetch.is_code_path("src/app/main.ts")
    assert not code_fetch.is_code_path("node_modules/x/index.js")
    assert not code_fetch.is_code_path("package-lock.json")
    assert not code_fetch.is_code_path("assets/logo.png")


def test_cut_excerpt():
    text = "a\n    b\n    c\nd"
    assert code_fetch.cut_excerpt(text, 2, 3) == "b\nc"
    assert code_fetch.cut_excerpt(text, 3, 2) is None
    assert code_fetch.cut_excerpt(text, 1, 9) is None


def test_code_stage_replaces_matched_blocks_with_file_lines(ctx, monkeypatch):
    ctx.save_model(metadata_stage.OUTPUT, _meta())
    ctx.save_model(generate.OUTPUT, _article())
    monkeypatch.setattr(code_fetch, "list_files", lambda c, repo: ("main", ["src/main.ts", "README.md"]))
    monkeypatch.setattr(code_fetch, "fetch_file", lambda repo, branch, path: "import x\nconst a = signal(1);\n")

    def fake_ask(c, prompt, content, shape):
        if shape is code_fetch.FilePicks:
            return code_fetch.FilePicks(picks=[code_fetch.FilePick(block_id="0.0", files=["octo/app:src/main.ts", "octo/app:invented.ts"])])
        return code_fetch.Excerpts(excerpts=[
            code_fetch.Excerpt(block_id="0.0", file="octo/app:src/main.ts", start_line=2, end_line=2),
            code_fetch.Excerpt(block_id="1.0", file=None, start_line=0, end_line=0),
        ])

    monkeypatch.setattr(code_fetch, "_ask", fake_ask)
    code_fetch.run(ctx)

    article = ctx.load_model(generate.OUTPUT, Article)
    matched, unmatched = article.sections[0].code_blocks[0], article.sections[1].code_blocks[0]
    assert (matched.code, matched.source, matched.needs_verification) == ("const a = signal(1);", "repo", False)
    assert (unmatched.code, unmatched.source, unmatched.needs_verification) == ("npm i", "video", True)
    report = ctx.load_json(code_fetch.OUTPUT)
    assert report["matches"][0]["video_code"] == "const a = signal(1)"
    assert report["unmatched"] == ["1.0"]


def test_code_stage_skips_unreadable_repo(ctx, monkeypatch):
    ctx.save_model(metadata_stage.OUTPUT, _meta())
    ctx.save_model(generate.OUTPUT, _article())

    def private(c, repo):
        raise httpx.HTTPStatusError("404", request=httpx.Request("GET", "u"), response=httpx.Response(404))

    monkeypatch.setattr(code_fetch, "list_files", private)
    monkeypatch.setattr(code_fetch, "_ask", lambda *a: pytest.fail("no LLM call without files"))
    code_fetch.run(ctx)
    assert ctx.load_json(code_fetch.OUTPUT) == {"repos": [], "matches": [], "unmatched": ["0.0", "1.0"]}


# --- Ghost update in place -------------------------------------------------------------

def test_check_updatable():
    ghost.check_updatable({"status": "draft", "updated_at": "T1"}, "T1")
    with pytest.raises(ghost.GhostConflict, match="edited"):
        ghost.check_updatable({"status": "draft", "updated_at": "T2"}, "T1")
    with pytest.raises(ghost.GhostConflict, match="published"):
        ghost.check_updatable({"status": "published", "updated_at": "T1"}, "T1")


def _ghost_ready(ctx, tmp_path, monkeypatch, current: dict | None):
    ctx.settings = Settings(data_dir=tmp_path, ghost_admin_api_key="ab:" + "a1" * 32)
    ctx.save_model(metadata_stage.OUTPUT, _meta())
    ctx.save_model(generate.OUTPUT, _article())
    ctx.path(render.HTML_OUTPUT).write_text("<p>new</p>", encoding="utf-8")
    ctx.save_json(render.JSONLD_OUTPUT, [])
    ctx.save_json(ghost.OUTPUT, {"id": "p1", "updated_at": "T1"})
    ctx.jobs.upsert(VID, ghost_post_id="p1")
    sent = {}
    monkeypatch.setattr(ghost.GhostAdmin, "get_post", lambda self, post_id: current)
    monkeypatch.setattr(ghost.GhostAdmin, "update_post",
                        lambda self, post_id, post: sent.update(post) or {"id": post_id, "slug": "t", "updated_at": "T2"})
    monkeypatch.setattr(ghost.GhostAdmin, "create_post", lambda self, post: pytest.fail("must not create"))
    return sent


def test_ghost_updates_untouched_draft(ctx, tmp_path, monkeypatch):
    sent = _ghost_ready(ctx, tmp_path, monkeypatch, {"status": "draft", "updated_at": "T1"})
    ghost.run(ctx)
    assert sent["html"] == "<p>new</p>" and sent["updated_at"] == "T1"
    assert "feature_image" not in sent and "authors" not in sent
    assert ctx.load_json(ghost.OUTPUT)["updated_at"] == "T2"


def test_ghost_refuses_draft_edited_in_ghost(ctx, tmp_path, monkeypatch):
    _ghost_ready(ctx, tmp_path, monkeypatch, {"status": "draft", "updated_at": "T9"})
    with pytest.raises(ghost.GhostConflict):
        ghost.run(ctx)


# --- Job status ------------------------------------------------------------------------

def test_status_recomputed_and_old_error_cleared(ctx):
    ctx.jobs.upsert(VID, status="failed", failed_stage="generate", error="boom")
    finish(ctx)
    assert ctx.jobs.get(VID)["status"] == "pending"
    ctx.path(render.OUTPUT).write_text("x", encoding="utf-8")
    finish(ctx)
    job = ctx.jobs.get(VID)
    assert (job["status"], job["failed_stage"], job["error"]) == ("article_ready", None, None)


def test_failed_stage_marks_job(ctx):
    with pytest.raises(StageFailed):
        run_stage(ctx, get_stage("render"))  # no article.json yet
    job = ctx.jobs.get(VID)
    assert (job["status"], job["failed_stage"]) == ("failed", "render")
