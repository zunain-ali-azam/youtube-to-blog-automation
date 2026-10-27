import pytest
from youtube_transcript_api import TranscriptsDisabled

from explainer.models import Chapter, Transcript, VideoMetadata
from explainer.stages import transcript
from explainer.stages.transcript import AutoSource, get_source, glossary_prompt, whisper_terms

WHISPER = Transcript(method="whisper", language="en", segments=[])
CAPTIONS = Transcript(method="captions", language="en", segments=[])


def test_get_source_knows_every_method():
    assert [get_source(m).name for m in ("captions", "whisper", "auto")] == ["captions", "whisper", "auto"]
    with pytest.raises(ValueError):
        get_source("nope")


def test_glossary_prompt():
    assert glossary_prompt(["Angular", "RxJS"]) == "Glossary: Angular, RxJS."
    assert glossary_prompt([]) is None


def test_glossary_prompt_drops_whole_terms_past_the_limit(monkeypatch):
    monkeypatch.setattr(transcript, "MAX_PROMPT_CHARS", 25)
    assert glossary_prompt(["Angular", "RxJS", "Signals"]) == "Glossary: Angular, RxJS."


def test_whisper_terms_puts_video_names_first_without_repeats():
    metadata = VideoMetadata(
        video_id="abc", title="Angular  Signals in 10 minutes", description="", published_at="",
        duration_iso="PT10M", duration_seconds=600, tags=["angular", "NgRx"], channel_title="Code with Ahsan",
        channel_id="c", thumbnail_url="", chapters=[Chapter(start_seconds=0, title="Intro")],
    )
    assert whisper_terms(["Angular", "RxJS"], metadata) == [
        "Angular Signals in 10 minutes", "Code with Ahsan", "Intro", "angular", "NgRx", "RxJS",
    ]
    assert whisper_terms(["Angular"], None) == ["Angular"]


def test_auto_prefers_captions(monkeypatch):
    monkeypatch.setattr(transcript.CaptionsSource, "fetch", lambda self, ctx: CAPTIONS)
    monkeypatch.setattr(transcript.WhisperSource, "fetch", lambda self, ctx: pytest.fail("whisper should not run"))
    assert AutoSource().fetch(None).method == "captions"


def test_auto_falls_back_to_whisper(monkeypatch):
    def no_captions(self, ctx):
        raise TranscriptsDisabled("abc")

    monkeypatch.setattr(transcript.CaptionsSource, "fetch", no_captions)
    monkeypatch.setattr(transcript.WhisperSource, "fetch", lambda self, ctx: WHISPER)
    assert AutoSource().fetch(None).method == "whisper"
