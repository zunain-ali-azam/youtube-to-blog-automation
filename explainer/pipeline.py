"""Runs stages in order. A stage whose output file already exists is skipped, so a
failed job resumes from the stage that failed."""

import logging
from dataclasses import dataclass
from typing import Callable

from explainer.config import ConfigError
from explainer.context import Context
from explainer.stages import checks, code_fetch, generate, ghost, metadata, render, transcript

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Stage:
    name: str
    output: str
    run: Callable[[Context], None]


STAGES = [
    Stage("metadata", metadata.OUTPUT, metadata.run),
    Stage("transcript", transcript.OUTPUT, transcript.run),
    Stage("generate", generate.OUTPUT, generate.run),
    Stage("code", code_fetch.OUTPUT, code_fetch.run),
    Stage("checks", checks.OUTPUT, checks.run),
    Stage("render", render.OUTPUT, render.run),
    Stage("ghost", ghost.OUTPUT, ghost.run),
]
STAGE_NAMES = [s.name for s in STAGES]


class StageFailed(Exception):
    def __init__(self, stage: str, error: BaseException):
        super().__init__(f"Stage '{stage}' failed: {error}")
        self.stage = stage


def run_stage(ctx: Context, stage: Stage) -> None:
    log.info("Stage %s: running", stage.name)
    ctx.jobs.upsert(ctx.video_id, status="running", current_stage=stage.name)
    try:
        stage.run(ctx)
    except KeyboardInterrupt:
        # Otherwise the job would stay "running" forever.
        ctx.jobs.upsert(ctx.video_id, status="failed", failed_stage=stage.name, error="Interrupted")
        raise
    except Exception as exc:
        if isinstance(exc, ConfigError):
            log.error("Stage %s failed: %s", stage.name, exc)
        else:
            log.exception("Stage %s failed", stage.name)
        ctx.jobs.upsert(ctx.video_id, status="failed", failed_stage=stage.name, error=f"{type(exc).__name__}: {exc}")
        raise StageFailed(stage.name, exc) from exc
    finish(ctx)


def finish(ctx: Context) -> None:
    """Set the job's status from the files on disk and clear any earlier failure."""
    job = ctx.jobs.get(ctx.video_id) or {}
    if job.get("status") == "published":
        status = "published"
    elif ctx.path(ghost.OUTPUT).exists():
        status = "draft_created"
    elif ctx.path(render.OUTPUT).exists():
        # Without Ghost the job ends at article.md.
        status = "article_ready"
    else:
        status = "pending"
    ctx.jobs.upsert(ctx.video_id, status=status, current_stage=None, failed_stage=None, error=None)


def _ignore(name: str, event: str) -> None:
    pass


def run_all(ctx: Context, force: bool = False, on_stage: Callable[[str, str], None] = _ignore) -> None:
    """Run every stage. `on_stage(name, event)` reports progress for the terminal UI; event
    is running, done, skipped (the stage chose to write nothing), cached or failed."""
    if force:
        # Clear cached outputs so every stage runs again.
        for stage in STAGES:
            ctx.path(stage.output).unlink(missing_ok=True)
    for stage in STAGES:
        if ctx.path(stage.output).exists():
            log.info("Stage %s: cached, skipping", stage.name)
            on_stage(stage.name, "cached")
            continue
        on_stage(stage.name, "running")
        try:
            run_stage(ctx, stage)
        except (StageFailed, KeyboardInterrupt):
            on_stage(stage.name, "failed")
            raise
        on_stage(stage.name, "done" if ctx.path(stage.output).exists() else "skipped")
    finish(ctx)


def get_stage(name: str) -> Stage:
    for stage in STAGES:
        if stage.name == name:
            return stage
    raise KeyError(f"Unknown stage {name!r}; choose from {', '.join(STAGE_NAMES)}")
