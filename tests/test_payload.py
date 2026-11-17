import json

import httpx
import pytest

from explainer.config import ConfigError, Settings
from explainer.context import Context
from explainer.jobs import JobStore
from explainer.pipeline import finish, stages_for
from explainer.stages import generate, metadata as metadata_stage, payload, render
from test_sanity import VID, _article, _meta


def _text(node: dict) -> str:
    return "".join(child["text"] if child["type"] == "text" else _text(child) if child["type"] == "link"
                   else "\n" if child["type"] == "linebreak" else "" for child in node["children"])


# --- Lexical ---------------------------------------------------------------------

def test_paragraph_formats_and_links():
    [para] = payload.html_to_lexical(
        '<p>Use <strong>bold</strong>, <em><b>both</b></em> and <code>x()</code> per <a href="https://a.dev">the <em>docs</em></a>.</p>')
    assert para["type"] == "paragraph"
    children = [(c["type"], c.get("text"), c.get("format")) for c in para["children"]]
    assert children == [("text", "Use ", 0), ("text", "bold", 1), ("text", ", ", 0), ("text", "both", 3),
                        ("text", " and ", 0), ("text", "x()", 16), ("text", " per ", 0), ("link", None, ""),
                        ("text", ".", 0)]
    link = para["children"][7]
    assert link["fields"] == {"linkType": "custom", "url": "https://a.dev", "newTab": False}
    assert [(c["text"], c["format"]) for c in link["children"]] == [("the ", 0), ("docs", 2)]


def test_headings_lists_nest_and_whitespace_between_blocks_is_dropped():
    nodes = payload.html_to_lexical(
        "<h3>Title</h3>\n<ul>\n <li>One</li>\n <li>Two<ol><li>Inner</li></ol></li>\n</ul>\n<p>After</p>")
    assert [n["type"] for n in nodes] == ["heading", "list", "paragraph"]
    assert nodes[0]["tag"] == "h3" and _text(nodes[0]) == "Title"
    outer = nodes[1]
    assert (outer["listType"], outer["tag"]) == ("bullet", "ul")
    one, two, wrapper = outer["children"]
    assert [_text(one), _text(two)] == ["One", "Two"]
    assert [item["value"] for item in outer["children"]] == [1, 2, 3]
    [inner] = wrapper["children"]
    assert (inner["type"], inner["listType"], _text(inner["children"][0])) == ("list", "number", "Inner")


def test_br_and_bare_text():
    [para] = payload.html_to_lexical("line one<br>line two")
    assert para["type"] == "paragraph" and _text(para) == "line one\nline two"


def test_build_content_order_and_blocks():
    content = payload.build_content(_article(), _meta())
    root = content["root"]
    assert root["type"] == "root"
    kinds = [n["fields"]["blockType"] if n["type"] == "block" else n.get("tag") or n["type"] for n in root["children"]]
    assert kinds == ["paragraph", "h2", "ul", "youtube", "h2", "paragraph", "paragraph", "code",
                     "h2", "ul", "h2", "h3", "paragraph", "paragraph"]
    nodes = root["children"]
    assert _text(nodes[4]) == "Creating <signals>"
    watch = nodes[5]["children"][0]
    assert watch["type"] == "link" and watch["fields"]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=272s"
    assert nodes[3]["fields"]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    code = nodes[7]["fields"]
    assert (code["language"], code["code"]) == ("typescript", "const a = signal(1);")
    assert len(code["id"]) == 24 and code["id"] != nodes[3]["fields"]["id"]
    assert _text(nodes[-1]).startswith("This article is based on the video Signals & <You>")


def test_build_fields():
    settings = Settings(site_url="https://example.com/blog")
    fields = payload.build_fields(_article(), _meta(), [{"@type": "VideoObject"}], {"root": {}}, settings)
    assert fields["slug"] == "angular-signals"
    assert fields["meta"] == {"title": "Angular Signals", "description": "m" * 130}
    assert fields["canonicalUrl"] == "https://example.com/blog/angular-signals"
    assert json.loads(fields["jsonLd"]) == [{"@type": "VideoObject"}]
    assert "canonicalUrl" not in payload.build_fields(_article(), _meta(), [], {}, Settings())


# --- stage -----------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path) -> Context:
    settings = Settings(data_dir=tmp_path, payload_url="http://localhost:3000/", payload_api_key="key")
    ctx = Context(VID, settings, JobStore(tmp_path / "jobs.db"), "captions", destination="payload")
    ctx.save_model(metadata_stage.OUTPUT, _meta())
    ctx.save_model(generate.OUTPUT, _article())
    ctx.save_json(render.JSONLD_OUTPUT, [])
    return ctx


class FakePayload:
    """Stands in for the HTTP calls; `docs` maps id -> {"updatedAt", "_status"}."""

    def __init__(self, monkeypatch, docs: dict[str, dict]):
        self.docs = docs
        self.writes: list[tuple] = []
        self.clock = 0
        monkeypatch.setattr(payload.PayloadClient, "get", lambda _, doc_id, draft: self.get(doc_id, draft))
        monkeypatch.setattr(payload.PayloadClient, "create", lambda _, data: self.create(data))
        monkeypatch.setattr(payload.PayloadClient, "update", lambda _, doc_id, data: self.update(doc_id, data))
        monkeypatch.setattr(payload.PayloadClient, "upload_image", lambda _, data, filename, alt: "media1")
        monkeypatch.setattr(payload.net, "request", lambda method, url, **kw: httpx.Response(200, content=b"jpg"))

    def _touch(self, doc_id: str, status: str) -> dict:
        self.clock += 1
        self.docs[doc_id] = {"id": doc_id, "updatedAt": f"t{self.clock}", "_status": status}
        return self.docs[doc_id]

    def get(self, doc_id, draft):
        return self.docs.get(doc_id)

    def create(self, data):
        self.writes.append(("create", data))
        return self._touch(f"p{len(self.writes)}", "draft")

    def update(self, doc_id, data):
        self.writes.append(("update", doc_id, data))
        return self._touch(doc_id, self.docs[doc_id]["_status"])


def test_creates_draft(ctx, monkeypatch):
    fake = FakePayload(monkeypatch, {})
    payload.run(ctx)
    [(kind, doc)] = fake.writes
    assert kind == "create" and doc["_status"] == "draft"
    assert doc["heroImage"] == "media1" and doc["meta"]["image"] == "media1"
    assert doc["title"] == "Angular Signals explained" and doc["content"]["root"]["children"]
    saved = ctx.load_json(payload.OUTPUT)
    assert saved == {"id": "p1", "updated_at": "t1", "editor_url": "http://localhost:3000/admin/collections/posts/p1"}
    assert ctx.jobs.get(VID)["payload_document_id"] == "p1"
    finish(ctx)
    assert ctx.jobs.get(VID)["status"] == "draft_created"


def test_create_applies_field_map(ctx, monkeypatch):
    ctx.settings = ctx.settings.model_copy(update={"payload_fields": "content=body,heroImage=-,tags=-"})
    fake = FakePayload(monkeypatch, {})
    payload.run(ctx)
    doc = fake.writes[0][1]
    assert "body" in doc and not {"content", "heroImage", "tags"} & set(doc)


def _pushed(ctx, updated_at: str = "t0") -> None:
    ctx.jobs.upsert(VID, payload_document_id="d1")
    ctx.save_json(payload.OUTPUT, {"id": "d1", "editor_url": None, "updated_at": updated_at})


def test_updates_untouched_draft_keeping_hero_image(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakePayload(monkeypatch, {"d1": {"updatedAt": "t0", "_status": "draft"}})
    payload.run(ctx)
    [(kind, doc_id, data)] = fake.writes
    assert (kind, doc_id) == ("update", "d1")
    assert data["title"] == "Angular Signals explained"
    assert "heroImage" not in data and "image" not in data["meta"] and "_status" not in data
    assert ctx.load_json(payload.OUTPUT)["updated_at"] == "t1"


def test_refuses_draft_edited_in_admin(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakePayload(monkeypatch, {"d1": {"updatedAt": "t9", "_status": "draft"}})
    with pytest.raises(payload.PayloadConflict, match="edited"):
        payload.run(ctx)
    assert fake.writes == []


def test_refuses_published_post(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakePayload(monkeypatch, {"d1": {"updatedAt": "t0", "_status": "published"}})
    with pytest.raises(payload.PayloadConflict, match="published"):
        payload.run(ctx)
    assert fake.writes == []


def test_recreates_deleted_post(ctx, monkeypatch):
    _pushed(ctx)
    fake = FakePayload(monkeypatch, {})
    payload.run(ctx)
    assert fake.writes[0][0] == "create"
    assert ctx.jobs.get(VID)["payload_document_id"] != "d1"


@pytest.mark.parametrize("update, destination", [({}, "sanity"), ({"payload_api_key": ""}, "payload")])
def test_skipped_unless_chosen_and_configured(ctx, monkeypatch, update, destination):
    ctx.settings = ctx.settings.model_copy(update=update)
    ctx.destination = destination
    fake = FakePayload(monkeypatch, {})
    payload.run(ctx)
    assert fake.writes == [] and not ctx.path(payload.OUTPUT).exists()


# --- HTTP ------------------------------------------------------------------------

def _recorder(monkeypatch, routes):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        for (want_method, part), response in routes.items():
            if method == want_method and part in url:
                return response(method, url, kwargs) if callable(response) else httpx.Response(200, json=response)
        raise AssertionError(f"unexpected {method} {url}")

    monkeypatch.setattr(payload.net, "request", fake_request)
    return calls


def test_client_with_api_key(monkeypatch):
    def not_found(method, url, kwargs):
        response = httpx.Response(404, request=httpx.Request(method, url))
        raise httpx.HTTPStatusError("not found", request=response.request, response=response)

    calls = _recorder(monkeypatch, {
        ("POST", "/api/media"): {"doc": {"id": "m1"}},
        ("POST", "/api/posts"): {"doc": {"id": "p1"}},
        ("PATCH", "/api/posts/p1"): {"doc": {"id": "p1"}},
        ("GET", "/api/posts/p1"): {"id": "p1", "_status": "draft", "updatedAt": "t1"},
        ("GET", "/api/posts/gone"): not_found,
    })
    client = payload.PayloadClient(Settings(payload_url="http://cms.test", payload_api_key="k"))
    assert client.upload_image(b"img", "v-max.jpg", "Alt") == "m1"
    assert client.create({"title": "T"})["id"] == "p1"
    client.update("p1", {"title": "U"})
    assert client.state("p1") == {"draft": {"id": "p1", "_status": "draft", "updatedAt": "t1"}, "published": False}
    assert client.state("gone") == {"draft": None, "published": False}
    assert client.editor_url("p1") == "http://cms.test/admin/collections/posts/p1"

    upload, create, update, *_ = calls
    assert upload[2]["headers"] == {"Authorization": "users API-Key k"}
    assert upload[2]["files"]["file"][0] == "v-max.jpg" and upload[2]["files"]["file"][2] == "image/jpeg"
    assert json.loads(upload[2]["data"]["_payload"]) == {"alt": "Alt"}
    assert create[2]["params"]["draft"] == "true" and create[2]["json"] == {"title": "T"}
    assert update[0] == "PATCH" and update[2]["params"]["draft"] == "true"


def test_client_logs_in_once_with_email_and_password(monkeypatch):
    calls = _recorder(monkeypatch, {
        ("POST", "/api/users/login"): {"token": "jwt1"},
        ("GET", "/api/posts/p1"): {"id": "p1", "_status": "published", "updatedAt": "t1"},
    })
    client = payload.PayloadClient(Settings(payload_email="a@b.c", payload_password="pw"))
    assert client.state("p1")["published"] is True
    login, *gets = calls
    assert login[1] == "http://localhost:3000/api/users/login"
    assert login[2]["json"] == {"email": "a@b.c", "password": "pw"}
    assert len(gets) == 2 and all(call[2]["headers"] == {"Authorization": "JWT jwt1"} for call in gets)


def _bad_request(body):
    def respond(method, url, kwargs):
        response = httpx.Response(400, json=body, request=httpx.Request(method, url))
        raise httpx.HTTPStatusError("bad request", request=response.request, response=response)
    return respond


def test_duplicate_slug_is_a_conflict(monkeypatch):
    _recorder(monkeypatch, {("POST", "/api/posts"): _bad_request(
        {"errors": [{"name": "ValidationError", "message": "The following field is invalid: slug",
                     "data": {"collection": "posts", "errors": [{"message": "Value must be unique", "path": "slug"}]}}]})})
    client = payload.PayloadClient(Settings(payload_api_key="k"))
    with pytest.raises(payload.PayloadConflict, match="slug 'my-post' already exists"):
        client.create({"slug": "my-post"})


def test_other_validation_errors_name_the_fields(monkeypatch):
    _recorder(monkeypatch, {("PATCH", "/api/posts/p1"): _bad_request(
        {"errors": [{"message": "x", "data": {"errors": [{"message": "This field is required", "path": "content"}]}}]})})
    client = payload.PayloadClient(Settings(payload_api_key="k"))
    with pytest.raises(payload.PayloadRejected, match="content: This field is required"):
        client.update("p1", {})


def test_client_requires_credentials():
    with pytest.raises(ConfigError, match="PAYLOAD_API_KEY"):
        payload.PayloadClient(Settings(payload_email="a@b.c"))


def test_stages_for_payload():
    assert [s.name for s in stages_for("payload")][-2:] == ["render", "payload"]


def test_conflict_is_logged_without_traceback(ctx, monkeypatch, caplog):
    from explainer.pipeline import StageFailed, get_stage, run_stage

    _pushed(ctx)
    FakePayload(monkeypatch, {"d1": {"updatedAt": "t9", "_status": "draft"}})
    with pytest.raises(StageFailed):
        run_stage(ctx, get_stage("payload"))
    [record] = [r for r in caplog.records if r.name == "explainer.pipeline"]
    assert "edited" in record.getMessage() and record.exc_info is None
    assert ctx.jobs.get(VID)["status"] == "failed"
