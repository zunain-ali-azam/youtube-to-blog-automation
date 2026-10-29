# Video Explainer Pipeline: YouTube → Blog Article (Markdown, optional Ghost Draft)

## Goal

Build a command-line tool that takes a YouTube video URL and produces a review-ready **blog article** as a Markdown file (`article.md`), with the video embedded, timestamps linked, author credited, and SEO metadata in the front matter. **Ghost is optional:** when it is configured, the article is also posted as a Ghost **draft**, a human reviews and publishes it in Ghost, and publishing triggers follow-up actions via webhook. Without Ghost, `article.md` is the final result.

The flow is: paste URL → fetch metadata → fetch captions or transcribe → one LLM call with all the information (metadata, chapters, transcript) → `article.md` → Ghost draft if Ghost is linked.

```
yt2blog run https://www.youtube.com/watch?v=VIDEO_ID
```

## Context

- The videos come from the **Code with Ahsan** YouTube channel (technical tutorials: Angular, React, JavaScript, AI, Google Gemini, Google ADK). Ahsan is a co-founder of IOMechs, and the articles will eventually live on the IOMechs Ghost site.
- **This is currently a practice build** using my personal accounts: a local Ghost install, my own Google Cloud project, and my own Slack (optional). Nothing should be published publicly. All credentials and site-specific values must come from config so switching to the IOMechs production setup is only a config change.
- Articles must be genuinely restructured written explainers, **not transcripts**. The video is embedded and linked as a companion.

## Working instructions for Claude Code

- Build in the phases listed at the bottom. Finish and test each phase before starting the next.
- Ask me before adding major dependencies or deviating from this spec.
- Check current docs for any library whose API may have changed (notably `youtube-transcript-api`, which changed its interface in v1.x, and the Ghost Admin API).
- Never hardcode secrets. Provide `.env.example` and make sure `.env` and `data/` are in `.gitignore`.

## Tech stack

- **Python 3.11+** (transcription tools are Python-native)
- `httpx` for HTTP, `pydantic` for data models and LLM output validation, `typer` for the CLI, `python-dotenv` for config
- `PyJWT` for Ghost Admin API auth
- `yt-dlp` + `ffmpeg` for audio download; `faster-whisper` for local transcription; `youtube-transcript-api` for caption fetching
- `anthropic` SDK for the LLM steps (model name set via env var)
- `fastapi` + `uvicorn` for the webhook receiver (Phase 4)
- SQLite (stdlib `sqlite3`) for job state

## Configuration (`.env`)

```
# YouTube
YOUTUBE_API_KEY=
YOUTUBE_UPDATE_DESCRIPTION=false      # practice mode: log only, never call the API

# Transcription
TRANSCRIPT_METHOD=captions            # captions | whisper | auto (captions, fall back to whisper)
WHISPER_MODEL=small                   # tiny | base | small | medium | large-v3 | turbo
WHISPER_DEVICE=auto                   # auto | cpu | cuda
WHISPER_COMPUTE_TYPE=int8

# LLM
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=

# Ghost (optional: leave GHOST_ADMIN_API_KEY empty to only produce article.md)
GHOST_URL=http://localhost:2368
GHOST_ADMIN_API_KEY=                  # format id:secret
GHOST_CONTENT_API_KEY=
GHOST_AUTHOR_EMAIL=                   # staff user to credit (placeholder "Ahsan Test" in practice)
GHOST_PRIMARY_TAG=video-explainer

# GitHub (optional, raises rate limits)
GITHUB_TOKEN=

# Slack (optional)
SLACK_WEBHOOK_URL=

# Webhook server
WEBHOOK_SECRET=
```

Also keep a `config/glossary.txt` of technical terms (one per line: Angular, Signals, RxJS, Gemini, ADK, standalone components, etc.). It is used by Whisper's `hotwords` hint and by the article generation prompt (to fix misheard terms).

## Project structure

```
explainer/
  cli.py              # typer commands
  config.py           # loads and validates env
  jobs.py             # SQLite job state
  models.py           # pydantic models
  stages/
    metadata.py       # Stage 1
    transcript.py     # Stage 2
    generate.py       # Stage 3
    code_fetch.py     # Stage 3a
    checks.py         # Stage 3b
    render.py         # Stage 4
    ghost.py          # Stage 5 (Ghost Admin + Content API client, optional)
    notify.py         # Stage 6 (Slack)
  webhook/server.py   # Stage 7
prompts/
  generate_article.md
  style_guide.md
config/glossary.txt
data/<video_id>/      # cached artifacts per video (gitignored)
tests/
```

## Pipeline stages

Each stage reads its inputs from and writes its output to `data/<video_id>/`, so any stage can be rerun or resumed without repeating earlier ones. The job status in SQLite is updated after each stage.

### Stage 0: Intake and idempotency

- Accept standard, short (`youtu.be`), and `shorts` URLs; extract the video ID.
- If a job for this video already exists with a Ghost post, stop and report it, unless `--force` is passed.

### Stage 1: Metadata → `metadata.json`

- YouTube Data API v3 `videos.list` with `part=snippet,contentDetails`.
- Store title, description, publish date, duration (parse ISO 8601), tags, channel name, highest-resolution thumbnail URL.
- Parse chapters from the description (lines starting with timestamps like `0:00 Intro` or `1:02:15 Deploying`).
- Extract GitHub repo URLs from the description.

### Stage 2: Transcript → `transcript_raw.json`

Common output format: a list of segments `{start: float, end: float, text: str}` plus the method used.

- **captions**: fetch public captions with `youtube-transcript-api` (prefer manual English captions over auto-generated).
- **whisper**: download audio with yt-dlp (`-x --audio-format mp3`), transcribe with `faster-whisper` using `vad_filter=True` and `hotwords` built from the video's title, channel, chapters and tags plus the glossary (`condition_on_previous_text=False`, so the hint and the output fit in Whisper's 448-token window). Delete the audio file after.
- **auto**: try captions, fall back to whisper on failure.
- Design this behind a common interface so a third method (official captions via YouTube OAuth, for when the channel owner authorizes it) can be added later. Do not implement OAuth now.

### Stage 3: Generate article → `article.json`

- One LLM call with `prompts/generate_article.md` + `prompts/style_guide.md` + the glossary, the raw transcript, metadata, and chapters. There is no separate transcript cleanup pass: the model reads past filler words and fixes misheard technical terms (using the glossary) while it writes.
- Merge the transcript segments into chunks of about a minute, each labelled with its start time, so the model can set each section's `start_seconds`.
- When Ghost is configured, also pass a list of existing posts (title, slug, tags) from the Ghost Content API so the model can suggest 2 to 4 relevant internal links.
- Output must be JSON matching this pydantic model, validated; on invalid output, retry up to 2 times with the validation error included.

```python
class Section(BaseModel):
    heading: str
    body_html: str            # paragraphs, lists; code goes in code_blocks
    start_seconds: int        # where this topic begins in the video
    code_blocks: list[CodeBlock] = []

class CodeBlock(BaseModel):
    language: str             # typescript, javascript, html, bash...
    code: str
    source: Literal["video", "repo"] = "video"
    needs_verification: bool = True

class Article(BaseModel):
    title: str                # not just the video title
    slug: str
    meta_title: str           # <= 60 chars
    meta_description: str     # 120-155 chars
    excerpt: str
    intro_html: str
    learning_points: list[str]
    sections: list[Section]
    key_takeaways: list[str]
    faq: list[FAQItem] = []
    tags: list[str]
    internal_links: list[InternalLink] = []
```

Article requirements (put in the prompt): restructured for readers, not a transcript; clear intro stating what the reader will learn; one section per concept, generally following the chapters; no long passages copied from the transcript; mention the software versions used if stated in the video.

### Stage 3a: Code from repo

- If a GitHub repo was found, list its files through the GitHub API and ask the LLM to match code blocks to repo files. Replace matched blocks with the real file contents (or the relevant excerpt) and set `source="repo"`, `needs_verification=False`.
- Unmatched blocks stay flagged.

### Stage 3b: Quality checks → `checks.json`

Produce a list of warnings (do not fail the job):

- Word count below 600 or above 3,000
- Meta title/description length out of range
- Any 25+ word sequence appearing verbatim in the transcript (n-gram overlap check)
- Code blocks still marked `needs_verification`
- Broken links (HEAD request on all external links)
- Missing intro, takeaways, or fewer than 2 sections

### Stage 4: Render → `article.md`, `article.html`, `jsonld.json`

`article.md` is the main deliverable: a complete blog post that can be read, edited, or pasted into any CMS. It is rendered from `article.json` (the model returns structured JSON, not Markdown, so every SEO field and timestamp is validated):

- YAML front matter: title, slug, meta title, meta description, excerpt, tags, feature image (thumbnail URL), video URL/title/publish date, channel, and the VideoObject JSON-LD.
- Body in the same order as the HTML below, with a responsive `<iframe>` embed, `[▶ Watch this part (4:32)](...)` links, and fenced code blocks.
- The article's HTML fields are a small fixed subset (`p`, `ul`, `ol`, `li`, `strong`, `em`, `code`, `a`), converted to Markdown with the standard library; no Markdown dependency.

`article.html` and `jsonld.json` are what the Ghost stage posts (Ghost's Admin API takes HTML, not Markdown). Build the final HTML in this order:

1. Intro and "What you'll learn" list
2. YouTube embed, wrapped so Ghost keeps it as an HTML card:
   ```html
   <!--kg-card-begin: html-->
   <iframe src="https://www.youtube.com/embed/VIDEO_ID" ...responsive, title, allowfullscreen></iframe>
   <!--kg-card-end: html-->
   ```
3. Sections, each with a link like `Watch this part (4:32)` pointing to `https://www.youtube.com/watch?v=VIDEO_ID&t=272s`
4. Code as `<pre><code class="language-xxx">` (escaped)
5. Key takeaways, FAQ
6. Credit block: video by Ahsan, links to the YouTube channel and the video

Also build the **VideoObject JSON-LD** (name, description, thumbnailUrl, uploadDate, duration, embedUrl, contentUrl) for the post's `codeinjection_head`.

### Stage 5: Create Ghost draft (optional)

- Skipped when `GHOST_ADMIN_API_KEY` is empty; the job then ends as `article_ready` with `article.md` as its result. When Ghost is configured, the job ends as `draft_created`.
- Auth: split the Admin API key into `id:secret`, hex-decode the secret, sign an HS256 JWT with header `kid=id`, payload `iat`, `exp` (5 minutes), `aud="/admin/"`. Send as `Authorization: Ghost <token>`. Generate a fresh token per request.
- Upload the thumbnail via `POST /ghost/api/admin/images/upload/` (multipart) and use the returned URL as `feature_image`.
- Create the post via `POST /ghost/api/admin/posts/?source=html` with `status: "draft"`, title, slug, html, `custom_excerpt`, `meta_title`, `meta_description`, `feature_image`, tags (primary tag first), author by email, and `codeinjection_head`.
- Store the Ghost post ID and admin editor URL in the job.
- Verify exact field names against the current Ghost Admin API docs.

### Stage 6: Notify

- If `SLACK_WEBHOOK_URL` is set, post: article title, Ghost editor link, transcript method, and all warnings from Stage 3b. Otherwise print the same to the terminal.

### Stage 7: Publish webhook (`yt2blog serve`)

- FastAPI endpoint receiving Ghost's `post.published` webhook. Verify the signature if `WEBHOOK_SECRET` is configured.
- Ignore posts without the primary tag. Match the post to its job by Ghost post ID.
- On match: mark the job published with the live URL; if `YOUTUBE_UPDATE_DESCRIPTION=true`, append the article link to the video description (not implemented yet: requires OAuth; log "would update description with: URL"); notify Slack.

## CLI commands

```
yt2blog run <url> [--force] [--transcript captions|whisper|auto]
yt2blog stage <stage_name> <video_id>      # rerun one stage from cache
yt2blog status [<video_id>]                # show job(s)
yt2blog compare-transcripts <url>          # run captions and whisper, save both + a diff report
yt2blog serve [--port 8000]                # webhook receiver
```

## Error handling

- Retry network calls with exponential backoff (3 attempts).
- On a stage failure, mark the job `failed` with the stage name and error, notify, and exit non-zero. `yt2blog run` on a failed job resumes from the failed stage.
- Log to console and `data/<video_id>/log.txt`.

## Testing

- Unit tests for URL parsing, chapter parsing, ISO 8601 duration parsing, JWT generation, n-gram overlap check, HTML rendering, and Markdown rendering.
- Mock all external APIs in unit tests.
- Provide a manual end-to-end test procedure against a local Ghost install.

## Build phases

1. **MVP:** Stages 0 to 5 with captions only. A URL produces `article.md`, and a Ghost draft when Ghost is configured.
2. **Transcription:** Whisper, `auto` mode, glossary prompt, `compare-transcripts` command.
3. **Quality:** Stages 3a, 3b, 6, internal links.
4. **Publishing loop:** Stage 7 webhook server.

## Manual Ghost setup (I'll do this, just document it in the README)

- Install Ghost locally (`ghost install local` or Docker).
- Settings → Integrations → Add custom integration: copy Admin and Content API keys; add a webhook for "Post published" pointing to the webhook server (use ngrok if needed).
- Create a placeholder staff user for the author.
- Settings → Code injection: add Prism.js (CSS + JS) for syntax highlighting.

## Out of scope for now

- YouTube OAuth (official captions, description updates)
- Auto-publishing without human review
- Batch processing of multiple URLs (keep the design compatible with it)
