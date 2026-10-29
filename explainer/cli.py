"""`yt2blog` command line."""

import json
import logging
import sys
import time
from pathlib import Path
from typing import Optional

import typer
from rich.logging import RichHandler
from rich.table import Table
from rich.text import Text
from youtube_transcript_api import YouTubeTranscriptApiException

from explainer import compare, ui, wizard
from explainer.config import ConfigError, Settings, load_settings
from explainer.context import Context
from explainer.jobs import JobStore
from explainer.pipeline import STAGE_NAMES, StageFailed, get_stage, run_all, run_stage
from explainer.stages import checks, generate, ghost as ghost_stage, render
from explainer.urls import parse_video_id

app = typer.Typer(add_completion=False, help="Turn a YouTube video into a blog article (Markdown, and a Ghost draft if Ghost is set up).")

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def _setup_logging(settings: Settings, video_id: str) -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    # The terminal shows progress itself; only warnings and errors go to the console.
    console = RichHandler(console=ui.console, level=logging.WARNING, show_time=False, show_path=False, markup=False)
    log_dir = settings.data_dir / video_id
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / "log.txt", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(console)
    root.addHandler(file_handler)
    # The HTTP client logs every request at INFO; keep that out of the console.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _video_id(value: str) -> str:
    try:
        return parse_video_id(value)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc


def _context(video_id: str, transcript: Optional[str], ghost: bool = True) -> Context:
    settings = load_settings()
    _setup_logging(settings, video_id)
    jobs = JobStore(settings.data_dir / "jobs.db")
    return Context(video_id, settings, jobs, transcript or settings.transcript_method, ghost=ghost)


def _print_checks(ctx: Context) -> None:
    if ctx.path(checks.OUTPUT).exists():
        ui.warnings(ctx.load_json(checks.OUTPUT))


def _summary(ctx: Context, seconds: float) -> None:
    article = ctx.load_json(generate.OUTPUT)
    body = ctx.path(render.OUTPUT).read_text(encoding="utf-8").split("---", 2)[-1]
    ui.alert("success", article["title"],
             f"{len(body.split()):,} words · {len(article['sections'])} sections · {ctx.settings.llm_model} · {ui.duration(seconds)}")
    steps = [("yt2blog", "make another blog"), ("yt2blog status", "see every blog made so far")]
    ui.alert("info", Text.assemble("Saved to  ", ui.link(render.blog_path(ctx.settings, article["slug"]))))
    if ctx.path(ghost_stage.OUTPUT).exists():
        _ghost_alert(ctx)
        steps.insert(0, ("open the Ghost link", "review the draft and publish it"))
    else:
        steps.insert(0, (f"yt2blog push {article['slug']}", "send it to Ghost as a draft"))
    _print_checks(ctx)
    ui.next_steps(steps)


def _ghost_alert(ctx: Context) -> None:
    editor_url = ctx.load_json(ghost_stage.OUTPUT)["editor_url"]
    ui.alert("info", Text.assemble("Ghost draft  ", (editor_url, f"{ui.ACCENT} link {editor_url}")))


def _run_pipeline(url: str, force: bool, transcript: Optional[str], ghost: bool = True, rail_open: bool = False) -> None:
    """Run every stage with live progress. `rail_open` is set when the guided run has already started the rail."""
    video_id = _video_id(url)
    ctx = _context(video_id, transcript, ghost)
    if not rail_open:
        ui.intro("yt2blog")

    job = ctx.jobs.get(video_id)
    if ghost and job and job.get("ghost_post_id") and not force:
        ui.outro("Nothing to do")
        ui.alert("info", "A Ghost draft already exists for this video", job.get("ghost_editor_url") or "")
        ui.next_steps([(f"yt2blog run {url} --force", "make a new draft instead")])
        return
    ctx.jobs.upsert(video_id, url=url)

    notes = {"transcript": ctx.transcript_method, "generate": ctx.settings.llm_model}
    started = time.monotonic()
    try:
        with ui.StageProgress(notes) as progress:
            run_all(ctx, force=force, on_stage=progress.update)
    except StageFailed as exc:
        ui.outro("Failed", ok=False)
        ui.alert("error", f"{ui.STAGE_LABELS.get(exc.stage, exc.stage)} failed", str(exc.__cause__ or exc))
        ui.alert("info", Text.assemble("Full log  ", ui.link(ctx.path("log.txt"))))
        ui.next_steps([("run the same command again", "after fixing it; finished stages are reused")])
        raise typer.Exit(1)
    except KeyboardInterrupt:
        ui.outro("Stopped", ok=False)
        ui.alert("info", "Run again to pick up where it left off")
        raise typer.Exit(130)
    ui.outro("Done")
    _summary(ctx, time.monotonic() - started)


@app.command()
def run(
    url: str,
    force: bool = typer.Option(False, "--force", help="Rerun every stage and create a new draft."),
    transcript: Optional[str] = typer.Option(None, "--transcript", help="captions | whisper | auto"),
    no_ghost: bool = typer.Option(False, "--no-ghost", help="Don't send to Ghost (the blog is still saved to generated_blogs/)."),
) -> None:
    """Run the whole pipeline for a video URL."""
    _run_pipeline(url, force, transcript, ghost=not no_ghost)


@app.command()
def start(
    reconfigure: bool = typer.Option(False, "--reconfigure", help="Ask for every API key again, even ones already saved."),
) -> None:
    """Guided run: step-by-step questions, then the whole pipeline."""
    if not sys.stdin.isatty():
        ui.console.print("The guided run needs an interactive terminal. Use [bold]yt2blog run <url>[/] instead.")
        raise typer.Exit(2)
    ui.banner()
    try:
        plan = wizard.ask(reconfigure)
    except KeyboardInterrupt:
        ui.outro("Cancelled. Nothing was run.", ok=False)
        raise typer.Exit(130)
    _run_pipeline(plan.url, force=False, transcript=plan.transcript, ghost=plan.ghost, rail_open=True)


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    """Running `yt2blog` with no command starts the guided run."""
    if ctx.invoked_subcommand is None:
        start(reconfigure=False)


@app.command()
def stage(
    name: str = typer.Argument(..., help=f"One of: {', '.join(STAGE_NAMES)}"),
    video_id: str = typer.Argument(...),
    transcript: Optional[str] = typer.Option(None, "--transcript", help="captions | whisper | auto"),
) -> None:
    """Rerun a single stage from cached inputs."""
    try:
        selected = get_stage(name)
    except KeyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    ctx = _context(_video_id(video_id), transcript)
    try:
        with ui.console.status(f"{ui.STAGE_LABELS[name]}…", spinner="dots"):
            run_stage(ctx, selected)
    except (StageFailed, ConfigError) as exc:
        ui.result_card([("Stage", name), ("Error", str(exc.__cause__ or exc))], ok=False, title="Failed")
        raise typer.Exit(1)
    ui.console.print(Text.assemble(("✓ ", "green"), (ui.STAGE_LABELS[name], "bold"), ("  ", ""), (str(ctx.path(selected.output)), "dim")))
    if name == "checks":
        _print_checks(ctx)


def _find_video(blog: str, settings: Settings) -> str:
    """The video behind a blog: its file or slug in generated_blogs/, or a video URL or ID."""
    slug = Path(blog).stem
    # Slugs first: an 11-character slug would also pass as a video ID.
    for article in settings.data_dir.glob(f"*/{generate.OUTPUT}"):
        if json.loads(article.read_text(encoding="utf-8")).get("slug") == slug:
            return article.parent.name
    try:
        return parse_video_id(blog)
    except ValueError:
        raise typer.BadParameter(f"No blog named {slug!r} in {settings.blogs_dir}, and it isn't a video URL or ID either")


@app.command()
def push(blog: str = typer.Argument(..., help="The blog's file or slug in generated_blogs/, or its video URL or ID.")) -> None:
    """Send a generated blog to Ghost as a draft, or update the draft it already has.

    Ghost gets the article as generated; edits made to the .md file aren't sent.
    """
    video_id = _find_video(blog, load_settings())
    ctx = _context(video_id, None)
    if not ctx.path(render.OUTPUT).exists():
        ui.alert("error", "That video has no finished blog yet", f"Run: yt2blog run {video_id}")
        raise typer.Exit(1)
    if not ghost_stage.is_configured(ctx.settings):
        ui.alert("error", "Ghost isn't set up", "Set GHOST_URL and GHOST_ADMIN_API_KEY in .env, or run `yt2blog` and pick Ghost")
        raise typer.Exit(1)
    try:
        with ui.spinner("Sending to Ghost"):
            run_stage(ctx, get_stage("ghost"))
    except (StageFailed, ConfigError) as exc:
        ui.alert("error", "Couldn't send it to Ghost", str(exc.__cause__ or exc))
        raise typer.Exit(1)
    ui.alert("success", ctx.load_json(generate.OUTPUT)["title"])
    _ghost_alert(ctx)


_STATUS_STYLES = {"failed": "red", "running": "yellow", "draft_created": "green", "article_ready": "green", "published": "bold green"}


@app.command()
def status(video_id: Optional[str] = typer.Argument(None)) -> None:
    """Show one job, or all jobs."""
    settings = load_settings()
    jobs = JobStore(settings.data_dir / "jobs.db")
    if video_id:
        job = jobs.get(_video_id(video_id))
        if not job:
            ui.console.print("No job for that video.")
            raise typer.Exit(1)
        keys = ("status", "url", "transcript_method", "failed_stage", "error", "ghost_editor_url", "published_url", "updated_at")
        rows = [(key.replace("_", " ").capitalize(), str(job[key])) for key in keys if job.get(key)]
        ui.result_card(rows, ok=job["status"] != "failed", title=job["video_id"])
        return
    all_jobs = jobs.list()
    if not all_jobs:
        ui.console.print("No jobs yet. Start one with [bold]explainer[/].")
        return
    table = Table(box=None, header_style="dim", pad_edge=False, padding=(0, 2))
    for column in ("Video", "Status", "Updated", "Detail"):
        table.add_column(column)
    for job in all_jobs:
        detail = job.get("error") or job.get("ghost_editor_url") or job.get("url") or ""
        table.add_row(job["video_id"], Text(job["status"], style=_STATUS_STYLES.get(job["status"], "")),
                      (job.get("updated_at") or "")[:16].replace("T", " "), Text(detail, style="dim", overflow="ellipsis", no_wrap=True))
    ui.console.print(table)


@app.command("compare-transcripts")
def compare_transcripts(
    url: str,
    force: bool = typer.Option(False, "--force", help="Fetch both transcripts again instead of reusing cached ones."),
) -> None:
    """Transcribe with Whisper and diff it against the captions."""
    video_id = _video_id(url)
    ctx = _context(video_id, None)
    try:
        with ui.console.status("Fetching captions and transcribing with Whisper…", spinner="dots"):
            result = compare.run(ctx, force=force)
    except YouTubeTranscriptApiException as exc:
        ui.console.print(f"[red]No captions to compare against[/] ({type(exc).__name__})")
        raise typer.Exit(1)
    except RuntimeError as exc:
        ui.console.print(f"[red]{exc}[/]")
        raise typer.Exit(1)
    ui.result_card([
        ("Differ", f"{result.error_rate:.1%} of words ({len(result.differences)} places)"),
        ("Report", ui.link(compare.report_path(ctx))),
    ], ok=True, title="Transcripts compared")


if __name__ == "__main__":
    app()
