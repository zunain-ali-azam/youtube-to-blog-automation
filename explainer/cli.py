"""`explainer` command line."""

import json
import logging
from pathlib import Path
from typing import Optional

import typer
from youtube_transcript_api import YouTubeTranscriptApiException

from explainer import compare
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
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
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


def _print_job(job: dict) -> None:
    typer.echo(f"{job['video_id']}  {job['status']}")
    for key in ("url", "transcript_method", "failed_stage", "error", "ghost_editor_url", "published_url", "updated_at"):
        if job.get(key):
            typer.echo(f"  {key}: {job[key]}")


def _print_checks(ctx: Context) -> None:
    if not ctx.path(checks.OUTPUT).exists():
        return
    warnings = ctx.load_json(checks.OUTPUT)
    if not warnings:
        typer.secho("  checks: no warnings", fg="green")
        return
    typer.secho(f"  checks: {len(warnings)} warning(s)", fg="yellow")
    for item in warnings:
        typer.echo(f"    - [{item['check']}] {item['message']}")


@app.command()
def run(
    url: str,
    force: bool = typer.Option(False, "--force", help="Rerun every stage and create a new draft."),
    transcript: Optional[str] = typer.Option(None, "--transcript", help="captions | whisper | auto"),
    no_ghost: bool = typer.Option(False, "--no-ghost", help="Don't send to Ghost (the blog is still saved to generated_blogs/)."),
) -> None:
    """Run the whole pipeline for a video URL."""
    video_id = _video_id(url)
    ctx = _context(video_id, transcript, ghost=not no_ghost)

    job = ctx.jobs.get(video_id)
    if ctx.ghost and job and job.get("ghost_post_id") and not force:
        typer.echo("A Ghost draft already exists for this video (use --force to make a new one):")
        _print_job(job)
        return
    ctx.jobs.upsert(video_id, url=url)

    try:
        run_all(ctx, force=force)
    except (StageFailed, ConfigError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    _print_job(ctx.jobs.get(video_id))
    slug = ctx.load_json(generate.OUTPUT)["slug"]
    typer.secho(f"  blog: {render.blog_path(ctx.settings, slug)}", fg="green")
    _print_checks(ctx)


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
        run_stage(ctx, selected)
    except (StageFailed, ConfigError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Stage {name} done: {ctx.path(selected.output)}")
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
        typer.secho(f"That video has no finished blog yet; run: explainer run {video_id}", fg="red", err=True)
        raise typer.Exit(1)
    if not ghost_stage.is_configured(ctx.settings):
        typer.secho("Ghost isn't set up: set GHOST_URL and GHOST_ADMIN_API_KEY in .env", fg="red", err=True)
        raise typer.Exit(1)
    try:
        run_stage(ctx, get_stage("ghost"))
    except (StageFailed, ConfigError) as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"Ghost draft: {ctx.load_json(ghost_stage.OUTPUT)['editor_url']}")


@app.command()
def status(video_id: Optional[str] = typer.Argument(None)) -> None:
    """Show one job, or all jobs."""
    settings = load_settings()
    jobs = JobStore(settings.data_dir / "jobs.db")
    if video_id:
        job = jobs.get(_video_id(video_id))
        if not job:
            typer.echo("No job for that video.")
            raise typer.Exit(1)
        _print_job(job)
        return
    all_jobs = jobs.list()
    if not all_jobs:
        typer.echo("No jobs yet.")
    for job in all_jobs:
        _print_job(job)


@app.command("compare-transcripts")
def compare_transcripts(
    url: str,
    force: bool = typer.Option(False, "--force", help="Fetch both transcripts again instead of reusing cached ones."),
) -> None:
    """Transcribe with Whisper and diff it against the captions."""
    video_id = _video_id(url)
    ctx = _context(video_id, None)
    try:
        result = compare.run(ctx, force=force)
    except YouTubeTranscriptApiException as exc:
        typer.secho(f"No captions to compare against ({type(exc).__name__})", fg="red", err=True)
        raise typer.Exit(1)
    except RuntimeError as exc:
        typer.secho(str(exc), fg="red", err=True)
        raise typer.Exit(1)
    typer.echo(f"{result.error_rate:.1%} of words differ ({len(result.differences)} places)")
    typer.echo(f"Report: {compare.report_path(ctx)}")


if __name__ == "__main__":
    app()
