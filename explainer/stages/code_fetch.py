"""Stage 3a: swap code transcribed from speech for the real code in the video's GitHub repo -> code_matches.json

For each repo linked in the description, the file list comes from the GitHub API.
Two LLM calls then do the matching: the first picks candidate files for each code
block from the paths alone, the second reads those files (with line numbers) and
returns the line range that holds each block's code. The new code is always cut
from the file itself, never written by the model, so a matched block is exactly
what is in the repo.

Matched blocks get `source="repo"` and `needs_verification=False`; article.json is
updated in place. Unmatched blocks stay flagged for the checks stage. A repo that
can't be read (private, deleted, rate limited) is skipped with a warning.
"""

import logging
import textwrap

import httpx
from pydantic import BaseModel

from explainer import llm, net
from explainer.config import load_prompt
from explainer.context import Context
from explainer.models import Article, CodeBlock, VideoMetadata
from explainer.stages import generate, metadata as metadata_stage

log = logging.getLogger(__name__)

OUTPUT = "code_matches.json"
API_URL = "https://api.github.com"
RAW_URL = "https://raw.githubusercontent.com"

MAX_PATHS = 1500  # paths listed to the model per repo
MAX_FILE_BYTES = 100_000
MAX_CANDIDATES = 12
MAX_CANDIDATE_CHARS = 300_000  # all candidate files together, sent with line numbers
MAX_EXCERPT_LINES = 150

_SKIP_DIRS = {"node_modules", "dist", "build", "out", "vendor", "coverage", ".git", ".next", ".angular",
              "__pycache__", ".venv", "venv"}
_SKIP_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "Cargo.lock",
               "go.sum", "composer.lock"}
_SKIP_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "ico", "bmp", "mp3", "mp4", "wav", "webm", "mov",
                    "woff", "woff2", "ttf", "otf", "eot", "pdf", "zip", "gz", "tar", "jar", "exe", "dll", "so",
                    "map", "lock", "bin", "pyc"}


# --- LLM response shapes ---------------------------------------------------------

class FilePick(BaseModel):
    block_id: str
    files: list[str]


class FilePicks(BaseModel):
    picks: list[FilePick]


class Excerpt(BaseModel):
    block_id: str
    file: str | None
    start_line: int
    end_line: int


class Excerpts(BaseModel):
    excerpts: list[Excerpt]


# --- GitHub ----------------------------------------------------------------------

def repo_name(url: str) -> str:
    """https://github.com/owner/repo -> owner/repo"""
    parts = url.split("github.com/", 1)[1].strip("/").split("/")
    return f"{parts[0]}/{parts[1]}"


def is_code_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    if any(part in _SKIP_DIRS for part in parts[:-1]) or name in _SKIP_FILES or name.endswith(".min.js"):
        return False
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return extension not in _SKIP_EXTENSIONS


def _headers(ctx: Context) -> dict:
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if ctx.settings.github_token:
        headers["Authorization"] = f"Bearer {ctx.settings.github_token}"
    return headers


def list_files(ctx: Context, repo: str) -> tuple[str, list[str]]:
    """The default branch and the repo's code files, smallest-path-depth first."""
    info = net.request("GET", f"{API_URL}/repos/{repo}", headers=_headers(ctx)).json()
    branch = info["default_branch"]
    tree = net.request("GET", f"{API_URL}/repos/{repo}/git/trees/{branch}",
                       params={"recursive": "1"}, headers=_headers(ctx)).json()
    paths = [entry["path"] for entry in tree["tree"]
             if entry["type"] == "blob" and entry.get("size", 0) <= MAX_FILE_BYTES and is_code_path(entry["path"])]
    if tree.get("truncated"):
        log.warning("GitHub truncated the file list of %s; matching against part of the repo", repo)
    # Shallow paths first, so a cut at MAX_PATHS keeps the top of the project.
    paths.sort(key=lambda p: (p.count("/"), p))
    return branch, paths[:MAX_PATHS]


def fetch_file(repo: str, branch: str, path: str) -> str:
    return net.request("GET", f"{RAW_URL}/{repo}/{branch}/{path}").text


# --- Matching --------------------------------------------------------------------

def pending_blocks(article: Article) -> dict[str, CodeBlock]:
    """Blocks still transcribed from the video, keyed "section.block" (both 0-based)."""
    return {
        f"{s}.{b}": block
        for s, section in enumerate(article.sections)
        for b, block in enumerate(section.code_blocks)
        if block.source == "video"
    }


def _blocks_text(article: Article, blocks: dict[str, CodeBlock]) -> str:
    out = []
    for block_id, block in blocks.items():
        heading = article.sections[int(block_id.split(".")[0])].heading
        out.append(f'<block id="{block_id}" language="{block.language}" section="{heading}">\n{block.code}\n</block>')
    return "\n\n".join(out)


def _ask(ctx: Context, prompt: str, content: str, shape: type[BaseModel]):
    text = llm.call(
        ctx.settings,
        system=load_prompt(prompt),
        messages=[{"role": "user", "content": content}],
        effort="medium",
        shape=shape,
    )
    return shape.model_validate_json(text)


def pick_files(ctx: Context, article: Article, blocks: dict[str, CodeBlock], files: dict[str, list[str]]) -> list[str]:
    listing = "\n\n".join(f'<repo name="{repo}">\n' + "\n".join(paths) + "\n</repo>" for repo, paths in files.items())
    picks = _ask(ctx, "code_pick_files.md",
                 f"<code_blocks>\n{_blocks_text(article, blocks)}\n</code_blocks>\n\n<files>\n{listing}\n</files>",
                 FilePicks)
    known = {f"{repo}:{path}" for repo, paths in files.items() for path in paths}
    chosen = []
    for pick in picks.picks:
        for file in pick.files:
            if file in known and file not in chosen:
                chosen.append(file)
    return chosen[:MAX_CANDIDATES]


def number_lines(text: str) -> str:
    return "\n".join(f"{n:>5}| {line}" for n, line in enumerate(text.splitlines(), 1))


def cut_excerpt(text: str, start_line: int, end_line: int) -> str | None:
    """Lines start_line..end_line (1-based, inclusive), dedented; None if the range is unusable."""
    lines = text.splitlines()
    if not 1 <= start_line <= end_line <= len(lines) or end_line - start_line + 1 > MAX_EXCERPT_LINES:
        return None
    excerpt = textwrap.dedent("\n".join(lines[start_line - 1:end_line])).strip("\n")
    return excerpt or None


def find_excerpts(ctx: Context, article: Article, blocks: dict[str, CodeBlock], contents: dict[str, str]) -> list[Excerpt]:
    files = "\n\n".join(f'<file name="{name}">\n{number_lines(text)}\n</file>' for name, text in contents.items())
    result = _ask(ctx, "code_excerpts.md",
                  f"<code_blocks>\n{_blocks_text(article, blocks)}\n</code_blocks>\n\n<files>\n{files}\n</files>",
                  Excerpts)
    return result.excerpts


def run(ctx: Context) -> None:
    article = ctx.load_model(generate.OUTPUT, Article)
    meta = ctx.load_model(metadata_stage.OUTPUT, VideoMetadata)
    blocks = pending_blocks(article)
    report = {"repos": [], "matches": [], "unmatched": list(blocks)}

    if not blocks or not meta.github_urls:
        log.info("Code: nothing to match (%d code blocks, %d GitHub URLs)", len(blocks), len(meta.github_urls))
        ctx.save_json(OUTPUT, report)
        return

    files: dict[str, list[str]] = {}
    branches: dict[str, str] = {}
    for url in meta.github_urls:
        repo = repo_name(url)
        try:
            branches[repo], files[repo] = list_files(ctx, repo)
        except httpx.HTTPError as exc:
            log.warning("Code: can't read GitHub repo %s (%s), skipping it", repo, exc)
            continue
        report["repos"].append(repo)
        log.info("Code: %s@%s has %d code files", repo, branches[repo], len(files[repo]))

    candidates = pick_files(ctx, article, blocks, files) if files else []
    contents: dict[str, str] = {}
    total = 0
    for name in candidates:
        repo, path = name.split(":", 1)
        try:
            text = fetch_file(repo, branches[repo], path)
        except httpx.HTTPError as exc:
            log.warning("Code: can't fetch %s (%s)", name, exc)
            continue
        if total + len(text) > MAX_CANDIDATE_CHARS:
            break
        contents[name] = text
        total += len(text)
    log.info("Code: %d candidate files for %d blocks: %s", len(contents), len(blocks), ", ".join(contents))

    excerpts = find_excerpts(ctx, article, blocks, contents) if contents else []
    for excerpt in excerpts:
        block = blocks.get(excerpt.block_id)
        if block is None or block.source == "repo" or excerpt.file not in contents:
            continue
        code = cut_excerpt(contents[excerpt.file], excerpt.start_line, excerpt.end_line)
        if code is None:
            log.warning("Code: ignoring bad range %s:%d-%d for block %s",
                        excerpt.file, excerpt.start_line, excerpt.end_line, excerpt.block_id)
            continue
        report["matches"].append({
            "block": excerpt.block_id, "file": excerpt.file,
            "start_line": excerpt.start_line, "end_line": excerpt.end_line, "video_code": block.code,
        })
        block.code, block.source, block.needs_verification = code, "repo", False

    report["unmatched"] = [block_id for block_id, block in blocks.items() if block.source == "video"]
    ctx.save_model(generate.OUTPUT, article)
    # Written last: it is the file the pipeline checks to skip this stage.
    ctx.save_json(OUTPUT, report)
    log.info("Code: %d of %d blocks replaced with repo code", len(report["matches"]), len(blocks))
