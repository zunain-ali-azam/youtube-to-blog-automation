"""`explainer compare-transcripts`: captions vs Whisper for one video -> compare_report.md

Not a pipeline stage. It shows how far Whisper is from the captions, so you can choose a
Whisper model and spot terms that belong in the glossary. Manual captions count as the
reference (the rate is a word error rate); auto-generated ones don't, so the rate is
just how much the two disagree.
"""

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from explainer.context import Context
from explainer.models import Transcript
from explainer.stages import metadata
from explainer.stages.transcript import CaptionsSource, WhisperSource
from explainer.timestamps import format_timestamp

log = logging.getLogger(__name__)

CAPTIONS_OUTPUT = "transcript_captions.json"
# One Whisper transcript and report per model, so models can be compared on the same video.
WHISPER_OUTPUT = "transcript_whisper_{model}.json"
REPORT_OUTPUT = "compare_report_{model}.md"

CONTEXT_WORDS = 3  # unchanged words shown either side of a difference
MERGE_GAP = 2  # differences separated by this many matching words become one row
TOP_DIFFERENCES = 15

_WORD = re.compile(r"\w+(?:'\w+)*")
_BRACKETED = re.compile(r"\[[^\]]*\]")  # [Music], [Applause]


@dataclass
class Difference:
    start: float
    captions: str
    whisper: str


@dataclass
class Comparison:
    caption_words: int
    whisper_words: int
    substitutions: int = 0
    deletions: int = 0
    insertions: int = 0
    differences: list[Difference] = field(default_factory=list)
    frequent: list[tuple[str, str, int]] = field(default_factory=list)

    @property
    def error_rate(self) -> float:
        return (self.substitutions + self.deletions + self.insertions) / max(self.caption_words, 1)


def to_words(transcript: Transcript) -> list[tuple[str, float]]:
    """Lowercased words without punctuation, each with the start time of its segment."""
    words = []
    for segment in transcript.segments:
        text = _BRACKETED.sub(" ", segment.text.lower().replace("’", "'"))
        words.extend((word, segment.start) for word in _WORD.findall(text))
    return words


def compare(captions: Transcript, whisper: Transcript) -> Comparison:
    ref, hyp = to_words(captions), to_words(whisper)
    ref_text, hyp_text = [w for w, _ in ref], [w for w, _ in hyp]
    result = Comparison(caption_words=len(ref), whisper_words=len(hyp))
    pairs = Counter()

    # difflib's alignment is not a true minimum edit distance, so the rate is approximate,
    # but it stays fast on hour-long videos.
    opcodes = SequenceMatcher(None, ref_text, hyp_text, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "replace":
            result.substitutions += min(i2 - i1, j2 - j1)
            result.deletions += max(0, (i2 - i1) - (j2 - j1))
            result.insertions += max(0, (j2 - j1) - (i2 - i1))
            if i2 - i1 <= 3 and j2 - j1 <= 3:
                pairs[" ".join(ref_text[i1:i2]), " ".join(hyp_text[j1:j2])] += 1
        elif tag == "delete":
            result.deletions += i2 - i1
        elif tag == "insert":
            result.insertions += j2 - j1

    for i1, i2, j1, j2 in _merge(opcodes):
        start = ref[i1][1] if i1 < len(ref) else (hyp[j1][1] if j1 < len(hyp) else 0.0)
        result.differences.append(Difference(
            start=start,
            captions=_with_context(ref_text, i1, i2),
            whisper=_with_context(hyp_text, j1, j2),
        ))
    result.frequent = [(c, w, n) for (c, w), n in pairs.most_common(TOP_DIFFERENCES)]
    return result


def _merge(opcodes) -> list[tuple[int, int, int, int]]:
    """Join differences that are only a word or two apart into one span."""
    spans = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        if spans and i1 - spans[-1][1] <= MERGE_GAP and j1 - spans[-1][3] <= MERGE_GAP:
            spans[-1] = (spans[-1][0], i2, spans[-1][2], j2)
        else:
            spans.append((i1, i2, j1, j2))
    return spans


def _with_context(words: list[str], start: int, end: int) -> str:
    before = " ".join(words[max(0, start - CONTEXT_WORDS):start])
    changed = " ".join(words[start:end])
    after = " ".join(words[end:end + CONTEXT_WORDS])
    middle = f"**{changed}**" if changed else "∅"
    return " ".join(part for part in (before, middle, after) if part)


def render_report(video_id: str, captions: Transcript, whisper: Transcript, model: str, result: Comparison) -> str:
    kind = "auto-generated" if captions.is_generated else "manual"
    counts = f"{result.substitutions} substituted, {result.deletions} missing, {result.insertions} extra"
    if captions.is_generated:
        rate = (f"**Disagreement: {result.error_rate:.1%}** ({counts}). Both transcripts are machine-made, "
                "so neither is the reference: read the differences below to judge which is right.")
    else:
        rate = f"**Whisper word error rate against the manual captions: {result.error_rate:.1%}** ({counts})."

    lines = [
        f"# Transcript comparison: {video_id}",
        "",
        f"- Captions: {kind}, {captions.language}, {result.caption_words} words",
        f"- Whisper: model `{model}`, {whisper.language}, {result.whisper_words} words",
        "",
        rate,
        "",
        "Words are compared lowercased and without punctuation, so the rate is approximate. "
        "Numbers written as digits on one side and words on the other count as differences.",
        "",
    ]
    if result.frequent:
        lines += [
            "## Most frequent differences",
            "",
            "Technical terms that show up here are candidates for `config/glossary.txt`.",
            "",
            "| Captions | Whisper | Times |",
            "|---|---|---|",
            *(f"| {c} | {w} | {n} |" for c, w, n in result.frequent),
            "",
        ]
    lines += [f"## All differences ({len(result.differences)})", ""]
    if result.differences:
        lines += ["| Time | Captions | Whisper |", "|---|---|---|"]
        for d in result.differences:
            link = f"[{format_timestamp(d.start)}](https://youtu.be/{video_id}?t={int(d.start)})"
            lines.append(f"| {link} | {d.captions} | {d.whisper} |")
    else:
        lines.append("None: the transcripts match word for word.")
    return "\n".join(lines) + "\n"


def _cached(ctx: Context, filename: str, source, force: bool) -> Transcript:
    if ctx.path(filename).exists() and not force:
        log.info("Using cached %s", filename)
        return ctx.load_model(filename, Transcript)
    transcript = source.fetch(ctx)
    ctx.save_model(filename, transcript)
    return transcript


def report_path(ctx: Context):
    return ctx.path(REPORT_OUTPUT.format(model=_model_slug(ctx)))


def _model_slug(ctx: Context) -> str:
    return ctx.settings.whisper_model.replace("/", "_")


def run(ctx: Context, force: bool = False) -> Comparison:
    if not ctx.path(metadata.OUTPUT).exists():
        metadata.run(ctx)  # Whisper builds its hint from the video's title, chapters and tags
    captions = _cached(ctx, CAPTIONS_OUTPUT, CaptionsSource(), force)
    whisper = _cached(ctx, WHISPER_OUTPUT.format(model=_model_slug(ctx)), WhisperSource(), force)
    result = compare(captions, whisper)
    report = render_report(ctx.video_id, captions, whisper, ctx.settings.whisper_model, result)
    report_path(ctx).write_text(report, encoding="utf-8")
    return result
