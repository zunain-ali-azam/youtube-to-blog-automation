import json
import sqlite3

import httpx
import pytest

from explainer.config import ConfigError, Settings
from explainer.context import Context
from explainer.jobs import JobStore
from explainer.models import Article, CodeBlock, FAQItem, Section, VideoMetadata
from explainer.pipeline import finish, stages_for
from explainer.stages import generate, metadata as metadata_stage, render, sanity

VID = "dQw4w9WgXcQ"


def _meta(**update) -> VideoMetadata:
    fields = dict(
        video_id=VID, title="Signals & <You>", description="d", published_at="2025-03-01T14:00:00Z",
        duration_iso="PT10M", duration_seconds=600, channel_title="Dev Tutorials", channel_id="UC123",
        thumbnail_url="https://i.ytimg.com/vi/x/maxresdefault.jpg",
    )
    fields.update(update)
    return VideoMetadata(**fields)


def _article() -> Article:
    return Article(
        title="Angular Signals explained", slug="angular-signals", meta_title="Angular Signals",
        meta_description="m" * 130, excerpt="e", intro_html="<p>Intro with <strong>bold</strong></p>",
        learning_points=["Create a <code>signal()</code>"],
        sections=[Section(heading="Creating <signals>", body_html="<p>Body</p>", start_seconds=272,
                          code_blocks=[CodeBlock(language="TypeScript", code="const a = signal(1);")])],
        key_takeaways=["Signals are sync"],
        faq=[FAQItem(question="What is a signal?", answer_html="<p>A value.</p>")],
        tags=["Angular", "Signals"],
    )


def _texts(block: dict) -> str:
    return "".join(span["text"] for span in block["children"])


# --- Portable Text ---------------------------------------------------------------

def test_paragraph_marks_and_links():
    [block] = sanity.html_to_portable_text(
        '<p>Use <strong>bold</strong>, <em>it</em> and <code>x()</code> per <a href="https://a.dev">the docs</a>.</p>')
    assert block["style"] == "normal"
    [link] = block["markDefs"]
    assert (link["_type"], link["linkType"], link["href"]) == ("link", "href", "https://a.dev")
    spans = [(s["text"], s["marks"]) for s in block["children"]]
    assert spans == [("Use ", []), ("bold", ["strong"]), (", ", []), ("it", ["em"]), (" and ", []),
                     ("x()", ["code"]), (" per ", []), ("the docs", [link["_key"]]), (".", [])]


def test_lists_nest_and_whitespace_between_blocks_is_dropped():
    blocks = sanity.html_to_portable_text("<ul>\n <li>One</li>\n <li>Two<ol><li>Inner</li></ol></li>\n</ul>\n<p>After</p>")
    assert [(_texts(b), b.get("listItem"), b.get("level")) for b in blocks] == [
        ("One", "bullet", 1), ("Two", "bullet", 1), ("Inner", "number", 2), ("After", None, None)]


def test_br_keeps_line_break_and_bare_text_gets_a_block():
    [block] = sanity.html_to_portable_text("line one<br>line two")
    assert _texts(block) == "line one\nline two"


def test_build_content_order_and_custom_blocks():
    content = sanity.build_content(_article(), _meta())
    kinds = [b["style"] if b["_type"] == "block" else b["_type"] for b in content]
    assert kinds == ["normal", "h2", "normal", "youtube", "h2", "normal", "normal", "code",
                     "h2", "normal", "h2", "h3", "normal", "normal"]
    heading = content[4]
    assert _texts(heading) == "Creating <signals>"
    watch = content[5]
    assert _texts(watch) == "Watch this part (4:32)"
    assert watch["markDefs"][0]["href"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=272s"
    assert content[3] == {"_type": "youtube", "_key": content[3]["_key"], "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
    assert content[7]["language"] == "typescript" and content[7]["code"] == "const a = signal(1);"
    assert _texts(content[-1]).startswith("This article is based on the video Signals & <You>")
    keys = [b["_key"] for b in content] + [s["_key"] for b in content for s in b.get("children", [])]
    assert len(keys) == len(set(keys))


def test_related_links_rendered():
    content = sanity.build_content(_article(), _meta(), [("Other post", "https://blog.dev/other/")])
    related = content[-2]
    assert _texts(related) == "Other post" and related["listItem"] == "bullet"
    assert related["markDefs"][0]["href"] == "https://blog.dev/other/"


# --- fields ----------------------------------------------------------------------

def test_field_map():
    mapping = sanity.parse_field_map(" content=body , seo=- ")
    assert mapping == {"content": "body", "seo": None}
    assert sanity.apply_field_map({"content": 1, "seo": 2, "title": 3}, mapping) == {"body": 1, "title": 3}
    assert sanity.parse_field_map("") == {}
    with pytest.raises(ConfigError):
        sanity.parse_field_map("content")


def test_build_fields():
    settings = Settings(site_url="https://example.com/blog")
    fields = sanity.build_fields(_article(), _meta(), [{"@type": "VideoObject"}], [], settings)
    assert fields["slug"] == {"_type": "slug", "current": "angular-signals"}
    assert fields["seo"]["canonicalUrl"] == "https://example.com/blog/angular-signals"
    assert json.loads(fields["jsonLd"]) == [{"@type": "VideoObject"}]
    assert "canonicalUrl" not in sanity.build_fields(_article(), _meta(), [], [], Settings())["seo"]


# --- stage -----------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path) -> Context:
    settings = Settings(data_dir=tmp_path, sanity_project_id="abc123", sanity_api_token="tok",
                        sanity_studio_url="http://localhost:3333/")
    ctx = Context(VID, settings, JobStore(tmp_path / "jobs.db"), "captions", destination="sanity")
    ctx.save_model(metadata_stage.OUTPUT, _meta())
    ctx.save_model(generate.OUTPUT, _article())
    ctx.save_json(render.JSONLD_OUTPUT, [])
    return ctx


class FakeSanity:
    """Stands in for the HTTP calls; `docs` maps document id -> rev."""

    def __init__(self, monkeypatch, docs: dict[str, str]):
        self.docs = docs
        self.mutations: list[dict] = []
        monkeypatch.setattr(sanity.SanityClient, "versions", lambda _, doc_id: self.versions(doc_id))
        monkeypatch.setattr(sanity.SanityClient, "mutate", lambda _, mutations: self.mutate(mutations))
        monkeypatch.setattr(sanity.SanityClient, "upload_image", lambda _, data, filename: "image-abc-1280x720-jpg")
        monkeypatch.setattr(sanity.net, "request", lambda method, url, **kw: httpx.Response(200, content=b"jpg"))

    def versions(self, doc_id):
        draft_id = sanity.DRAFT_PREFIX + doc_id
        return {"draft": {"_id": draft_id, "_rev": self.docs[draft_id]} if draft_id in self.docs else None,
                "published": {"_id": doc_id, "_rev": self.docs[doc_id]} if doc_id in self.docs else None}

    def mutate(self, mutations):
        self.mutations += mutations
        for mutation in mutations:
            doc_id = mutation["create"]["_id"] if "create" in mutation else mutation["patch"]["id"]
            self.docs[doc_id] = f"rev{len(self.mutations)}"
        return {}


def test_creates_draft(ctx, monkeypatch):
    fake = FakeSanity(monkeypatch, {})
    sanity.run(ctx)
    [mutation] = fake.mutations
    doc = mutation["create"]
    assert doc["_id"].startswith("drafts.") and doc["_type"] == "post"
    assert doc["coverImage"]["asset"] == {"_type": "reference", "_ref": "image-abc-1280x720-jpg"}
    assert doc["coverImage"]["alt"] == "Signals & <You>"
    assert doc["title"] == "Angular Signals explained" and doc["content"] and doc["date"]
    doc_id = doc["_id"].removeprefix("drafts.")
    saved = ctx.load_json(sanity.OUTPUT)
    assert saved == {"id": doc_id, "draft_id": doc["_id"], "rev": "rev1",
                     "editor_url": f"http://localhost:3333/intent/edit/id={doc_id};type=post"}
    assert ctx.jobs.get(VID)["sanity_document_id"] == doc_id
    finish(ctx)
    assert ctx.jobs.get(VID)["status"] == "draft_created"


def test_create_applies_field_map(ctx, monkeypatch):
    ctx.settings = ctx.settings.model_copy(update={"sanity_fields": "content=body,coverImage=mainImage,seo=-",
                                                   "sanity_post_type": "article"})
    fake = FakeSanity(monkeypatch, {})
    sanity.run(ctx)
    doc = fake.mutations[0]["create"]
    assert doc["_type"] == "article" and "body" in doc and "mainImage" in doc
    assert not {"content", "coverImage", "seo"} & set(doc)


def _pushed(ctx, rev: str = "r1") -> None:
    ctx.jobs.upsert(VID, sanity_document_id="d1")
    ctx.save_json(sanity.OUTPUT, {"id": "d1", "draft_id": "drafts.d1", "editor_url": None, "rev": rev})


def test_updates_untouched_draft_keeping_cover_and_date(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakeSanity(monkeypatch, {"drafts.d1": "r1"})
    sanity.run(ctx)
    [mutation] = fake.mutations
    patch = mutation["patch"]
    assert patch["id"] == "drafts.d1" and patch["ifRevisionID"] == "r1"
    assert patch["set"]["title"] == "Angular Signals explained"
    assert "coverImage" not in patch["set"] and "date" not in patch["set"]
    assert ctx.load_json(sanity.OUTPUT)["rev"] == "rev1"


def test_refuses_draft_edited_in_studio(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakeSanity(monkeypatch, {"drafts.d1": "r9"})
    with pytest.raises(sanity.SanityConflict, match="edited"):
        sanity.run(ctx)
    assert fake.mutations == []


def test_refuses_published_post(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakeSanity(monkeypatch, {"d1": "r5"})
    with pytest.raises(sanity.SanityConflict, match="published"):
        sanity.run(ctx)
    assert fake.mutations == []


def test_recreates_deleted_draft(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakeSanity(monkeypatch, {})
    sanity.run(ctx)
    assert "create" in fake.mutations[0]
    assert ctx.jobs.get(VID)["sanity_document_id"] != "d1"


@pytest.mark.parametrize("update, destination", [({}, "ghost"), ({"sanity_api_token": ""}, "sanity")])
def test_skipped_unless_chosen_and_configured(ctx, monkeypatch, update, destination):
    ctx.settings = ctx.settings.model_copy(update=update)
    ctx.destination = destination
    fake = FakeSanity(monkeypatch, {})
    sanity.run(ctx)
    assert fake.mutations == [] and not ctx.path(sanity.OUTPUT).exists()


# --- HTTP ------------------------------------------------------------------------

def test_client_requests(monkeypatch):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if "/data/query/" in url:
            return httpx.Response(200, json={"result": [{"_id": "drafts.d1", "_rev": "r1"}]})
        if "/assets/images/" in url:
            return httpx.Response(200, json={"document": {"_id": "image-x-jpg"}})
        return httpx.Response(200, json={"transactionId": "t"})

    monkeypatch.setattr(sanity.net, "request", fake_request)
    client = sanity.SanityClient(Settings(sanity_project_id="abc123", sanity_api_token="tok"))
    assert client.versions("d1") == {"draft": {"_id": "drafts.d1", "_rev": "r1"}, "published": None}
    assert client.upload_image(b"img", "v-max.jpg") == "image-x-jpg"
    client.mutate([{"create": {"_id": "drafts.x"}}])
    assert client.editor_url("d1") is None

    (_, query_url, query), (_, upload_url, upload), (_, mutate_url, mutate) = calls
    assert query_url == "https://abc123.api.sanity.io/v2025-02-19/data/query/production"
    assert query["params"]["perspective"] == "raw"
    assert json.loads(query["params"]["$ids"]) == ["drafts.d1", "d1"]
    assert upload_url.endswith("/assets/images/production")
    assert upload["headers"]["Content-Type"] == "image/jpeg" and upload["params"] == {"filename": "v-max.jpg"}
    assert mutate_url.endswith("/data/mutate/production")
    assert mutate["headers"]["Authorization"] == "Bearer tok"


def test_revision_mismatch_is_a_conflict(monkeypatch):
    def conflict(method, url, **kwargs):
        response = httpx.Response(409, request=httpx.Request(method, url))
        raise httpx.HTTPStatusError("conflict", request=response.request, response=response)

    monkeypatch.setattr(sanity.net, "request", conflict)
    client = sanity.SanityClient(Settings(sanity_project_id="abc123", sanity_api_token="tok"))
    with pytest.raises(sanity.SanityConflict):
        client.mutate([])


def test_client_requires_config():
    with pytest.raises(ConfigError, match="SANITY_PROJECT_ID"):
        sanity.SanityClient(Settings(sanity_api_token="tok"))


# --- pipeline and jobs -------------------------------------------------------------

def test_stages_for_destination():
    names = lambda d: [s.name for s in stages_for(d)]  # noqa: E731
    assert names("sanity")[-2:] == ["render", "sanity"]
    assert names("ghost")[-1] == "ghost"
    assert names("none")[-1] == "render"


def test_old_jobs_db_gains_new_columns(tmp_path):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (video_id TEXT PRIMARY KEY, status TEXT, ghost_post_id TEXT, updated_at TEXT, created_at TEXT)")
    conn.execute("INSERT INTO jobs VALUES ('v1', 'draft_created', 'p1', 'x', 'x')")
    conn.commit()
    conn.close()
    jobs = JobStore(db)
    jobs.upsert("v1", sanity_document_id="d1")
    assert jobs.get("v1")["ghost_post_id"] == "p1" and jobs.get("v1")["sanity_document_id"] == "d1"
