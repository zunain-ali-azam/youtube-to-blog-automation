<p align="center">
  <img src="assets/logo.svg" width="140" alt="yt2blog logo">
</p>

<p align="center">
  Turn a <a href="https://www.youtube.com">YouTube</a> video into a review-ready blog post: Markdown, SEO metadata and a CMS draft, from one command.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/version-v0.2.0-d946ef" alt="version 0.2.0">
  <img src="https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/command-yt2blog-a21caf" alt="command: yt2blog">
  <img src="https://img.shields.io/badge/LLM-any%20provider-8b5cf6" alt="LLM: any provider">
  <img src="https://img.shields.io/badge/CMS-Ghost-15171a?logo=ghost&logoColor=white" alt="CMS: Ghost">
  <img src="https://img.shields.io/badge/transcripts-captions%20%7C%20Whisper-ff0000?logo=youtube&logoColor=white" alt="transcripts: captions or Whisper">
  <br>
  <img src="https://img.shields.io/badge/built%20with-Typer-009688" alt="built with Typer">
  <img src="https://img.shields.io/badge/UI-Rich-f472b6" alt="UI: Rich">
  <img src="https://img.shields.io/badge/tested%20with-pytest-0a9edc?logo=pytest&logoColor=white" alt="tested with pytest">
</p>

# YouTube to Blog Automation

`yt2blog` turns a YouTube video into a review-ready blog article: a restructured written explainer with the video embedded, timestamped "watch this part" links, SEO metadata and VideoObject JSON-LD. Every finished blog is saved as Markdown, and it can also go to a CMS as a **draft** for a human to review and publish.

The full spec is in [`video-explainer-pipeline.md`](video-explainer-pipeline.md).

## Contents

- [Features](#features)
- [Quick start](#quick-start)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [How it works](#how-it-works)
- [Destinations](#destinations)
- [Development](#development)
- [Roadmap](#roadmap)

---

## Features

- **Guided run**: run `yt2blog` with no arguments and it asks for keys, the video and the destination one step at a time, then shows the stages as a live checklist.
- **Any LLM provider**: Anthropic, OpenAI, Gemini, Groq, DeepSeek, OpenRouter, Mistral, Ollama, or any OpenAI-compatible API.
- **Captions or Whisper**: uses YouTube captions, or transcribes locally with faster-whisper when a video has none.
- **Real code, not invented code**: code blocks are matched to the video's GitHub repo and replaced with the actual lines.
- **Quality checks**: length, SEO fields, copied passages, unverified code, bad timestamps and broken links are all flagged as warnings.
- **SEO ready**: meta title and description, Open Graph and Twitter fields, canonical URL, and VideoObject and FAQPage JSON-LD.
- **Resumable**: every stage caches its output, so a failed run picks up where it stopped.
- **CMS drafts**: sends the post to Ghost as a draft, and updates that draft later without overwriting edits made in Ghost.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/yt2blog
```

The guided run creates `.env` for you and asks for the keys it needs. To skip the questions:

```bash
yt2blog run https://www.youtube.com/watch?v=VIDEO_ID
```

---

## Installation

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill it in (or let the guided run do it)
```

### Local Whisper (optional)

```bash
.venv/bin/pip install -e ".[whisper]"
```

This adds faster-whisper and yt-dlp. It's free and runs on your machine; the model downloads on first use (`small` is about 500 MB). No system ffmpeg is needed. yt-dlp works best with [deno](https://deno.com) installed, since YouTube extraction without a JavaScript runtime is deprecated.

## Configuration

Settings live in `.env` at the project root (see [`.env.example`](.env.example)). The minimum is `YOUTUBE_API_KEY` plus a key for your LLM provider.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `YOUTUBE_API_KEY` | | **Required.** YouTube Data API v3 key |
| `LLM_PROVIDER` | `anthropic` | Which LLM to use (see [LLM providers](#llm-providers)) |
| `LLM_MODEL` | `claude-opus-5-5` on Anthropic | Model name; required for every other provider |
| `LLM_BASE_URL` | | For `custom`, or to override a provider's URL |
| `TRANSCRIPT_METHOD` | `captions` | `captions`, `whisper`, or `auto` (captions when the video has them, Whisper otherwise) |
| `WHISPER_MODEL` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` or `turbo` |
| `WHISPER_DEVICE` | `auto` | `auto`, `cpu` or `cuda` |
| `WHISPER_COMPUTE_TYPE` | `int8` | faster-whisper compute type |
| `GHOST_URL` | `http://localhost:2368` | Ghost site |
| `GHOST_ADMIN_API_KEY` | | Ghost Admin API key (`id:secret`); empty means no Ghost draft |
| `GHOST_CONTENT_API_KEY` | | Enables internal links to published posts |
| `GHOST_AUTHOR_EMAIL` | | Staff user to credit; without it Ghost credits the site owner |
| `GHOST_PRIMARY_TAG` | `video-explainer` | First tag on every draft |
| `SITE_URL` | | Public blog base, e.g. `https://iomechs.com/blog`; sets the canonical URL to `SITE_URL/<slug>` |
| `GITHUB_TOKEN` | | Raises the GitHub API limit above 60 requests an hour |
| `BLOGS_DIR` | `generated_blogs/` | Where finished blogs are saved |
| `DATA_DIR` | `data/` | Where each video's stage files are cached |

`SLACK_WEBHOOK_URL`, `WEBHOOK_SECRET` and `YOUTUBE_UPDATE_DESCRIPTION` are reserved for planned features (see [Roadmap](#roadmap)).

### YouTube API key

Google Cloud Console → **APIs & Services** → enable *YouTube Data API v3* → **Credentials** → *Create API key*. Restrict the key to that API.

### LLM providers

`LLM_PROVIDER` picks the provider, and its key goes in that provider's own variable, so you can keep several keys and switch by changing `LLM_PROVIDER`. The guided run asks for the provider, then for its key if it isn't saved yet.

| `LLM_PROVIDER` | Key variable | Notes |
|---|---|---|
| `anthropic` (default) | `ANTHROPIC_API_KEY` | `LLM_MODEL` defaults to `claude-opus-5-5` |
| `openai` | `OPENAI_API_KEY` | |
| `gemini` | `GEMINI_API_KEY` | |
| `groq` | `GROQ_API_KEY` | |
| `deepseek` | `DEEPSEEK_API_KEY` | |
| `openrouter` | `OPENROUTER_API_KEY` | |
| `mistral` | `MISTRAL_API_KEY` | |
| `ollama` | none | Local; `http://localhost:11434/v1` |
| `custom` | `LLM_API_KEY` | Any other OpenAI-compatible API; also set `LLM_BASE_URL` (ending in `/v1`) |

Every provider except Anthropic is called through its OpenAI-compatible `/chat/completions` endpoint and needs `LLM_MODEL`. `LLM_BASE_URL` also overrides a named provider's URL (for example Ollama on another machine).

Article generation is a single call, with output constrained to the `Article` JSON schema and validated by pydantic, plus up to 2 retries that include the validation error. On Anthropic it runs at `high` effort and opts into the server-side refusal fallback (`fallbacks: "default"`), so a safety-classifier decline is retried on the recommended fallback model instead of failing the job. Other providers get the schema as `response_format` and in the system prompt, since not every compatible API enforces `response_format`; how reliably they return valid JSON depends on the model.

---

## Usage

| Command | What it does |
|---|---|
| `yt2blog` | Guided run (same as `yt2blog start`) |
| `yt2blog start --reconfigure` | Guided run that asks for every key again |
| `yt2blog run <url> [--force] [--no-ghost] [--transcript captions\|whisper\|auto]` | Whole pipeline, no questions |
| `yt2blog stage <stage> <video_id>` | Rerun one stage from cache |
| `yt2blog push <blog>` | Send a saved blog to Ghost, or update its draft |
| `yt2blog status [<video_id>]` | Show jobs |
| `yt2blog compare-transcripts <url> [--force]` | Captions vs Whisper report |

### Guided run

`yt2blog` opens with a welcome banner (block-letter logo, version, the commands and controls), then asks one step at a time down a single rail. Each answered step collapses to its answer (`◇  Model / │  claude-opus-5-5`), so the screen reads as a timeline:

1. **Keys and model**: the YouTube key, the LLM provider, that provider's key and the model. Saved keys show masked (`sk-a…hijk  (saved)`) and aren't asked again; the saved model is one Enter to keep. The model list comes live from the provider with your key, and long lists filter as you type. `.env` is created from `.env.example` if there isn't one, and what you enter is saved there.
2. **Video**: the URL (checked when you press Enter) and the transcript source. The Whisper options are greyed out until the Whisper extra is installed.
3. **Destination**: whether to also send the blog to Ghost as a draft (asking for the Ghost keys if they aren't saved yet). The blog is saved to `generated_blogs/` either way.

After **Start generating?**, the stages run as a live checklist on the same rail. The run ends with `SUCCESS`/`INFO`/`WARNING` lines (title, length, where the blog went, quality warnings) and the next commands to run. On a failure you get an `ERROR` line with the stage and the error, plus the log file; running again resumes from that stage. Ctrl+C or Esc at any step cancels without running anything.

### Saved blogs and Ghost drafts

Every run saves the finished blog to `generated_blogs/<slug>.md` (git-ignored). When Ghost is configured, `yt2blog run` also sends it to Ghost as a draft; `--no-ghost` skips that for one run.

`yt2blog push <blog>` sends a saved blog to Ghost later, or updates the draft it already has. `<blog>` is the file (`generated_blogs/my-post.md`), its slug (`my-post`) or the video's URL or ID. Ghost gets the article as generated: edits made to the `.md` file aren't sent.

`yt2blog stage ghost <video_id>` updates the job's existing draft (title, body, excerpt, SEO fields, tags and JSON-LD; the feature image and author stay as they are). It refuses when the post has been published, or edited in Ghost since the last push, because the update would overwrite those edits.

`--force` clears the cached files and makes a new draft, even if one already exists for the video.

### Comparing transcripts

`compare-transcripts` needs the Whisper extra. Use it to pick a `WHISPER_MODEL` before trusting Whisper on videos without captions.

It saves `transcript_captions.json`, `transcript_whisper_<model>.json` and `compare_report_<model>.md` to `data/<video_id>/`, one Whisper transcript and report per `WHISPER_MODEL`, so you can compare models on the same video. The report has the word error rate (or the disagreement rate, when the captions are auto-generated), the most frequent differences as glossary candidates, and every difference with a timestamp link. Both transcripts are cached; `--force` fetches them again.

---

## How it works

### Pipeline

`metadata` → `transcript` → `generate` → `code` → `checks` → `render` → `ghost`

| Stage | Output | What it does |
|---|---|---|
| `metadata` | `metadata.json` | Title, description, chapters and thumbnail from the YouTube API |
| `transcript` | `transcript_raw.json` | Captions or a Whisper transcript |
| `generate` | `article.json`, `ghost_posts.json` | One LLM call writes the article (see below) |
| `code` | `code_matches.json` | Swaps code blocks for the real lines from the repo |
| `checks` | `checks.json` | Quality warnings |
| `render` | `article.md`, `article.html`, `jsonld.json` | Final Markdown, HTML and JSON-LD; copies the post to `generated_blogs/<slug>.md` |
| `ghost` | `ghost_post.json` | Creates or updates the Ghost draft; skipped when Ghost isn't configured |

There is no separate transcript cleanup: `generate` sends the raw transcript, the metadata and the glossary in one LLM call and the model fixes misheard terms as it writes.

### Caching and reruns

Every stage writes to `data/<video_id>/`, along with a `log.txt` that includes token usage for each LLM call. `article.md` is the finished post: SEO fields in YAML front matter, then the article body in Markdown.

A stage whose output already exists is skipped, so rerunning `yt2blog run` on a failed job resumes from the stage that failed. To redo a single step (after editing a prompt, for example), delete its file and everything after it, or use `yt2blog stage`.

The folders in `data/` are named after the video ID (`dQw4w9WgXcQ`), because it's the one name known before the article exists and it never changes, so reruns find their cache. Look in `generated_blogs/` for the readable names.

### Quality stages

- **`code`** (needs a GitHub URL in the video description): lists the repo's files through the GitHub API, and two LLM calls match each code block to a file and a line range. Matched blocks are replaced with the lines cut from the real file (the model never writes the code), marked `source: "repo"` and no longer flagged. `code_matches.json` records each match with the code it replaced. Private or missing repos are skipped.
- **`checks`**: warnings only, never a failure. Word count (600 to 3,000), meta title/description and excerpt lengths, missing intro/takeaways/sections, 25+ word passages copied verbatim from the transcript, code blocks still unverified, section timestamps past the end of the video, HTML outside the allowed tag subset, internal links to unknown posts, and broken external links. The warnings print at the end of the run.
- **Internal links**: with `GHOST_CONTENT_API_KEY` set, `generate` passes the site's published posts to the model, which can pick 2 to 4 related ones. They render as a "Related articles" list; a slug that isn't a real published post is dropped.

### Prompts and glossary

Prompts live in `prompts/` and the technical glossary in `config/glossary.txt`. You can edit them without changing any code.

---

## Destinations

The Markdown file in `generated_blogs/` is always written. On top of that, the blog can be sent to a CMS as a draft:

| Destination | State |
|---|---|
| Ghost | **Supported** (see [Ghost setup](#ghost-setup)) |
| Sanity | Planned |
| Payload CMS | Planned |

### Ghost setup

1. **Install Ghost locally**: `npm i -g ghost-cli && mkdir ghost-local && cd ghost-local && ghost install local`, or run it with Docker (`docker run -d -p 2368:2368 -e NODE_ENV=development ghost`). Then open `http://localhost:2368/ghost` and create the owner account.
2. **Settings → Integrations → Add custom integration** ("Video Explainer"). Copy the **Admin API key** into `GHOST_ADMIN_API_KEY` and the **Content API key** into `GHOST_CONTENT_API_KEY`.
3. In the same integration, add a webhook for **Post published** pointing at the webhook server (Phase 4; use ngrok if Ghost can't reach your machine).
4. **Settings → Staff**: invite a placeholder user ("Ahsan Test") and put their email in `GHOST_AUTHOR_EMAIL`.
5. **Settings → Code injection → Site header/footer**: add Prism.js for syntax highlighting:
   ```html
   <!-- Site header -->
   <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css">
   <!-- Site footer -->
   <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-core.min.js"></script>
   <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
   ```

### Headless front ends

Set `SITE_URL` when another site displays the posts through the Ghost Content API: the draft's canonical URL becomes `SITE_URL/<slug>`. The draft also gets Open Graph and Twitter titles and descriptions (copied from the meta fields, since a headless front end has no theme to fall back on), and `codeinjection_head` holds the VideoObject and FAQPage JSON-LD, which that site has to put into its `<head>` itself.

---

## Development

### Tests

```bash
.venv/bin/pytest
```

Unit tests cover URL parsing, chapter parsing, ISO 8601 durations, JWT generation, transcript chunking, HTML/Markdown/JSON-LD rendering, the quality checks (including the n-gram overlap check), code matching, updating a Ghost draft in place, and job status. They make no network calls.

### Manual end-to-end test (local Ghost)

1. Complete the [Ghost setup](#ghost-setup) and fill in `.env`.
2. Pick a Code with Ahsan video that has captions and chapters, then run `yt2blog run <url>`.
3. Check `data/<video_id>/`. Every stage file should exist, and `log.txt` should show token usage for each LLM call.
4. Open the editor link that gets printed. In Ghost, check that:
   - the post is a **draft**, with the right title, slug, excerpt and the `video-explainer` tag first
   - the feature image is the thumbnail, uploaded to Ghost (not hotlinked from YouTube)
   - the video embed shows as an HTML card and plays
   - each "Watch this part" link jumps to the right moment
   - code blocks are highlighted after previewing (Prism)
   - Post settings → Meta data shows the meta title and description
   - Post settings → Code injection contains the VideoObject JSON-LD
5. Run `yt2blog run <url>` again. It should report the existing draft and do nothing.
6. Run `yt2blog status` to see the job as `draft_created`.
7. Without Ghost: leave `GHOST_ADMIN_API_KEY` empty, run `yt2blog run <url> --force`, and check that the job ends as `article_ready` and `generated_blogs/<slug>.md` has the front matter, embed, watch links and code blocks.

---

## Roadmap

| Phase | Scope | State |
|---|---|---|
| 1. MVP | Stages 0–5 with captions (Markdown output, optional Ghost draft) | **Done** |
| 2. Transcription | Whisper, `auto` mode, glossary prompt, `compare-transcripts` | **Done** |
| 3. Quality | Code from repo, quality checks, internal links (warnings print in the terminal) | **Done**, except Slack notify |
| 4. Publishing loop | `yt2blog serve` webhook | Not started |
| 5. More destinations | Sanity, Payload CMS | Not started |
