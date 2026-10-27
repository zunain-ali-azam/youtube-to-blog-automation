"""Stage 2: raw transcript -> transcript_raw.json

Every method implements `TranscriptSource` and returns the same `Transcript` shape,
so new methods (official captions via YouTube OAuth later) plug in through `get_source`.
"""

import logging
import tempfile
from pathlib import Path
from typing import Protocol

from youtube_transcript_api import NoTranscriptFound, YouTubeTranscriptApi, YouTubeTranscriptApiException

from explainer.config import load_glossary
from explainer.context import Context
from explainer.models import Segment, Transcript, VideoMetadata
from explainer.stages import metadata as metadata_stage

log = logging.getLogger(__name__)

OUTPUT = "transcript_raw.json"
ENGLISH = ["en", "en-US", "en-GB"]
# Whisper keeps at most ~223 hotword tokens and cuts the rest mid-term, so trim whole terms ourselves.
MAX_PROMPT_CHARS = 600


class TranscriptSource(Protocol):
    name: str

    def fetch(self, ctx: Context) -> Transcript: ...


class CaptionsSource:
    """Public YouTube captions. Prefers manually created English captions over auto-generated."""

    name = "captions"

    def fetch(self, ctx: Context) -> Transcript:
        transcripts = YouTubeTranscriptApi().list(ctx.video_id)
        try:
            chosen = transcripts.find_manually_created_transcript(ENGLISH)
        except NoTranscriptFound:
            chosen = transcripts.find_generated_transcript(ENGLISH)
        fetched = chosen.fetch()
        log.info("Captions: %s (%s), %d snippets",
                 chosen.language, "auto-generated" if chosen.is_generated else "manual", len(fetched))
        return Transcript(
            method=self.name,
            language=chosen.language_code,
            is_generated=chosen.is_generated,
            segments=to_segments([(s.start, s.duration, s.text) for s in fetched]),
        )


class WhisperSource:
    """Local transcription: download the audio with yt-dlp, transcribe with faster-whisper, delete the audio."""

    name = "whisper"

    def fetch(self, ctx: Context) -> Transcript:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise RuntimeError('Whisper needs extra packages: pip install -e ".[whisper]"') from exc

        settings = ctx.settings
        with tempfile.TemporaryDirectory(prefix="explainer-audio-") as tmp:
            audio = download_audio(ctx.video_id, Path(tmp))
            log.info("Whisper: model %s on %s (%s)",
                     settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
            model = WhisperModel(settings.whisper_model, device=settings.whisper_device,
                                 compute_type=settings.whisper_compute_type)
            prompt = glossary_prompt(whisper_terms(load_glossary(), _load_metadata(ctx)))
            log.info("Whisper hint: %s", prompt)
            # hotwords, unlike initial_prompt, is sent with every 30 s window, not just the first.
            # Prompt and output share 448 tokens per window, so don't also carry over the previous
            # window's text: hotwords + up to 223 previous tokens left ~70 for output, and Whisper
            # dropped most of each window's speech.
            segments, info = model.transcribe(str(audio), vad_filter=True, hotwords=prompt,
                                              condition_on_previous_text=False)
            # `segments` is a lazy generator: transcription happens while it is consumed.
            snippets = [(s.start, s.end - s.start, s.text) for s in segments]
        log.info("Whisper: language %s (p=%.2f), %d segments", info.language, info.language_probability, len(snippets))
        return Transcript(method=self.name, language=info.language, segments=to_segments(snippets))


class AutoSource:
    """Captions when the video has them, otherwise Whisper."""

    name = "auto"

    def fetch(self, ctx: Context) -> Transcript:
        try:
            return CaptionsSource().fetch(ctx)
        except YouTubeTranscriptApiException as exc:
            log.warning("Captions unavailable (%s), falling back to Whisper", type(exc).__name__)
        return WhisperSource().fetch(ctx)


def download_audio(video_id: str, dest: Path) -> Path:
    """Fetch the best audio-only stream as-is; faster-whisper decodes it, so no ffmpeg is needed."""
    from yt_dlp import YoutubeDL

    options = {
        "format": "bestaudio/best",
        "outtmpl": str(dest / "%(id)s.%(ext)s"),
        "quiet": True,
        "noprogress": True,
    }
    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}")
        path = Path(ydl.prepare_filename(info))
    log.info("Downloaded audio: %s (%.1f MB)", path.name, path.stat().st_size / 1e6)
    return path


def _load_metadata(ctx: Context) -> VideoMetadata | None:
    return ctx.load_model(metadata_stage.OUTPUT, VideoMetadata) if ctx.path(metadata_stage.OUTPUT).exists() else None


def whisper_terms(glossary: list[str], metadata: VideoMetadata | None) -> list[str]:
    """The video's own names (title, channel, chapters, tags) first, then the shared glossary, without repeats."""
    terms = []
    if metadata:
        terms += [metadata.title, metadata.channel_title, *(c.title for c in metadata.chapters), *metadata.tags]
    seen, unique = set(), []
    for term in terms + glossary:
        term = " ".join(term.split())
        if term and term.lower() not in seen:
            seen.add(term.lower())
            unique.append(term)
    return unique


def glossary_prompt(terms: list[str]) -> str | None:
    """Biases Whisper's spelling toward these terms; drops whole terms from the end past MAX_PROMPT_CHARS."""
    kept, length = [], len("Glossary: .")
    for term in terms:
        length += len(term) + (2 if kept else 0)
        if length > MAX_PROMPT_CHARS:
            break
        kept.append(term)
    return f"Glossary: {', '.join(kept)}." if kept else None


def to_segments(snippets: list[tuple[float, float, str]]) -> list[Segment]:
    """Caption snippets overlap on screen, so end each one where the next begins."""
    segments = []
    for i, (start, duration, text) in enumerate(snippets):
        end = start + duration
        if i + 1 < len(snippets):
            end = min(end, snippets[i + 1][0])
        text = " ".join(text.split())
        if text:
            segments.append(Segment(start=round(start, 2), end=round(end, 2), text=text))
    return segments


def get_source(method: str) -> TranscriptSource:
    sources = {"captions": CaptionsSource, "whisper": WhisperSource, "auto": AutoSource}
    if method not in sources:
        raise ValueError(f"Unknown transcript method {method!r}; use one of {', '.join(sources)}")
    return sources[method]()


def run(ctx: Context) -> None:
    transcript = get_source(ctx.transcript_method).fetch(ctx)
    ctx.save_model(OUTPUT, transcript)
    ctx.jobs.upsert(ctx.video_id, transcript_method=transcript.method)
