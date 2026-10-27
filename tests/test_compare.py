from explainer.compare import compare, render_report, to_words
from explainer.models import Segment, Transcript


def transcript(*texts: str, method="captions", generated=False) -> Transcript:
    segments = [Segment(start=i * 10.0, end=i * 10.0 + 10, text=t) for i, t in enumerate(texts)]
    return Transcript(method=method, language="en", is_generated=generated, segments=segments)


def test_to_words_normalizes_and_keeps_times():
    words = to_words(transcript("[Music] Hello, World!", "Don’t use RxJS."))
    assert words == [("hello", 0.0), ("world", 0.0), ("don't", 10.0), ("use", 10.0), ("rxjs", 10.0)]


def test_identical_transcripts():
    result = compare(transcript("Angular signals are great."), transcript("angular signals are great"))
    assert result.error_rate == 0 and result.differences == []


def test_counts_substitutions_deletions_insertions():
    captions = transcript("the elephants have really long trunks", "and that is all")
    whisper = transcript("the elephants have long prompts", "and that is all folks", method="whisper")
    result = compare(captions, whisper)
    assert (result.substitutions, result.deletions, result.insertions) == (1, 1, 1)
    assert result.caption_words == 10
    assert result.error_rate == 0.3
    assert ("trunks", "prompts", 1) in result.frequent
    assert [d.start for d in result.differences] == [0.0, 10.0]  # "really" and "trunks" merge


def test_nearby_differences_merge_into_one_row():
    captions = transcript("use angular signals in your components today")
    whisper = transcript("use angler signals in you components today", method="whisper")
    result = compare(captions, whisper)
    assert len(result.differences) == 1
    assert "**angular signals in your**" in result.differences[0].captions


def test_report_labels_reference_by_caption_kind():
    whisper = transcript("hi there", method="whisper")
    manual = render_report("vid", transcript("hi"), whisper, "small", compare(transcript("hi"), whisper))
    auto = render_report("vid", transcript("hi", generated=True), whisper, "small",
                         compare(transcript("hi", generated=True), whisper))
    assert "word error rate against the manual captions: 100.0%" in manual
    assert "Disagreement: 100.0%" in auto
    assert "[0:00](https://youtu.be/vid?t=0)" in manual

