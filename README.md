# Video Explainer Pipeline

Turns a YouTube video into a review-ready blog article: a restructured written explainer with the video embedded, timestamped "watch this part" links, SEO metadata and VideoObject JSON-LD. Every finished blog is saved to `generated_blogs/<slug>.md`. It can also go to Ghost as a **draft**, during the run or later with `explainer push`, and a human reviews and publishes it there.

The full spec is in [`video-explainer-pipeline.md`](video-explainer-pipeline.md).

```
explainer run https://www.youtube.com/watch?v=VIDEO_ID
```

## Status

| Phase | Scope | State |
|---|---|---|
| 1. MVP | Stages 0–5 with captions (Markdown output, optional Ghost draft) | **Done** |
| 2. Transcription | Whisper, `auto` mode, glossary prompt, `compare-transcripts` | **Done** |
| 3. Quality | Code from repo, quality checks, internal links (warnings print in the terminal) | **Done**, except Slack notify |
| 4. Publishing loop | `explainer serve` webhook | Not started |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env    # then fill it in
```

**Local Whisper (optional):** `.venv/bin/pip install -e ".[whisper]"` adds faster-whisper and yt-dlp. Set `TRANSCRIPT_METHOD=auto` to use captions when a video has them and transcribe locally when it doesn't, or `whisper` to always transcribe. It's free and runs on your machine; the model downloads on first use (`small` is about 500 MB). No system ffmpeg is needed. yt-dlp works best with [deno](https://deno.com) installed, since YouTube extraction without a JavaScript runtime is deprecated.

Phase 1 needs `YOUTUBE_API_KEY` and an API key for your LLM provider (see [LLM](#llm)). Ghost is optional: set `GHOST_URL` and `GHOST_ADMIN_API_KEY` to also get a Ghost draft, or leave the key empty to stop at the Markdown file. `GHOST_AUTHOR_EMAIL` is optional; without it, Ghost credits the site owner. Set `SITE_URL` to the public blog base (for example `https://iomechs.com/blog`) when another site displays the posts through the Content API: the draft's canonical URL becomes `SITE_URL/<slug>`. The draft also gets Open Graph and Twitter titles and descriptions (copied from the meta fields, since a headless front end has no theme to fall back on), and `codeinjection_head` holds the VideoObject and FAQPage JSON-LD, which that site has to put into its `<head>` itself.

**YouTube API key:** Google Cloud Console → APIs & Services → enable *YouTube Data API v3* → Credentials → *Create API key*. Restrict the key to that API.

## Usage

```bash
explainer run <url> [--force] [--no-ghost] [--transcript captions|whisper|auto]   # whole pipeline
explainer stage <stage> <video_id>                      # rerun one stage from cache
explainer push <blog>                                   # send a saved blog to Ghost
explainer status [<video_id>]                           # show jobs
explainer compare-transcripts <url> [--force]           # captions vs Whisper report
```

Every run saves the finished blog to `generated_blogs/<slug>.md` (git-ignored; `BLOGS_DIR` changes the folder). When Ghost is configured, `explainer run` also sends it to Ghost as a draft; `--no-ghost` skips that for one run. `explainer push <blog>` sends a saved blog to Ghost later, or updates the draft it already has. `<blog>` is the file (`generated_blogs/my-post.md`), its slug (`my-post`) or the video's URL or ID. Ghost gets the article as generated: edits made to the `.md` file aren't sent.

The folders in `data/` are named after the video ID (`dQw4w9WgXcQ`), because it's the one name known before the article exists and it never changes, so reruns find their cache. Look in `generated_blogs/` for the readable names.

`compare-transcripts` needs the Whisper extra. It saves `transcript_captions.json`, `transcript_whisper_<model>.json` and `compare_report_<model>.md` to `data/<video_id>/` (one Whisper transcript and report per `WHISPER_MODEL`, so you can compare models on the same video): the word error rate (or the disagreement rate, when the captions are auto-generated), the most frequent differences as glossary candidates, and every difference with a timestamp link. Both transcripts are cached; `--force` fetches them again. Use it to pick a `WHISPER_MODEL` before trusting Whisper on videos without captions.

Stages: `metadata` → `transcript` → `generate` → `code` → `checks` → `render` → `ghost` (skipped when Ghost isn't configured).

There is no separate transcript cleanup: `generate` sends the raw transcript, the metadata and the glossary in one LLM call and the model fixes misheard terms as it writes.

Every stage writes to `data/<video_id>/`: `metadata.json`, `transcript_raw.json`, `article.json` (plus `ghost_posts.json`, the posts offered for internal links), `code_matches.json`, `checks.json`, `article.md`, `article.html`, `jsonld.json`, `ghost_post.json` (only with Ghost) and `log.txt`. `article.md` is the finished post (SEO fields in YAML front matter, then the article body in Markdown), and the render stage copies it to `generated_blogs/<slug>.md`. A stage whose output already exists is skipped, so rerunning `explainer run` on a failed job resumes from the stage that failed. To redo a single step (after editing a prompt, for example), delete its file and everything after it, or use `explainer stage`.

`explainer stage ghost <video_id>` updates the job's existing draft (title, body, excerpt, SEO fields, tags and JSON-LD; the feature image and author stay as they are). It refuses when the post has been published, or edited in Ghost since the last push, because the update would overwrite those edits. Use `--force` on `explainer run` to get a new draft instead.

`--force` clears the cached files and makes a new draft, even if one already exists for the video.

### Quality stages

- **`code`** (needs a GitHub URL in the video description): lists the repo's files through the GitHub API, and two LLM calls match each code block to a file and a line range. Matched blocks are replaced with the lines cut from the real file (the model never writes the code), marked `source: "repo"` and no longer flagged. `code_matches.json` records each match with the code it replaced. `GITHUB_TOKEN` is optional; without it, GitHub allows 60 API requests an hour. Private or missing repos are skipped.
- **`checks`**: warnings only, never a failure. Word count (600 to 3,000), meta title/description and excerpt lengths, missing intro/takeaways/sections, 25+ word passages copied verbatim from the transcript, code blocks still unverified, section timestamps past the end of the video, HTML outside the allowed tag subset, internal links to unknown posts, and broken external links. The warnings print at the end of `explainer run`.
- **Internal links**: with `GHOST_CONTENT_API_KEY` set, `generate` passes the site's published posts to the model, which can pick 2 to 4 related ones. They render as a "Related articles" list; a slug that isn't a real published post is dropped.

Prompts live in `prompts/` and the technical glossary in `config/glossary.txt`. You can edit them without changing any code.

## LLM

Any LLM provider works. `LLM_PROVIDER` picks it, and its key goes in that provider's own variable, so you can keep several keys and switch by changing `LLM_PROVIDER`:

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

## Manual Ghost setup

1. Install Ghost locally: `npm i -g ghost-cli && mkdir ghost-local && cd ghost-local && ghost install local`, or run it with Docker (`docker run -d -p 2368:2368 -e NODE_ENV=development ghost`). Then open `http://localhost:2368/ghost` and create the owner account.
2. **Settings → Integrations → Add custom integration** ("Video Explainer"). Copy the **Admin API key** into `GHOST_ADMIN_API_KEY` and the **Content API key** into `GHOST_CONTENT_API_KEY`.
3. In the same integration, add a webhook for **Post published** pointing at the webhook server (Phase 4; use ngrok if Ghost can't reach your machine).
4. **Settings → Staff:** invite a placeholder user ("Ahsan Test") and put their email in `GHOST_AUTHOR_EMAIL`.
5. **Settings → Code injection → Site header/footer:** add Prism.js for syntax highlighting:
   ```html
   <!-- Site header -->
   <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/themes/prism-tomorrow.min.css">
   <!-- Site footer -->
   <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/components/prism-core.min.js"></script>
   <script src="https://cdnjs.cloudflare.com/ajax/libs/prism/1.29.0/plugins/autoloader/prism-autoloader.min.js"></script>
   ```

## Manual end-to-end test (local Ghost)

1. Complete the setup above and fill in `.env`.
2. Pick a Code with Ahsan video that has captions and chapters, then run `explainer run <url>`.
3. Check `data/<video_id>/`. Every stage file should exist, and `log.txt` should show token usage for each LLM call.
4. Open the editor link that gets printed. In Ghost, check that:
   - the post is a **draft**, with the right title, slug, excerpt and the `video-explainer` tag first
   - the feature image is the thumbnail, uploaded to Ghost (not hotlinked from YouTube)
   - the video embed shows as an HTML card and plays
   - each "Watch this part" link jumps to the right moment
   - code blocks are highlighted after previewing (Prism)
   - Post settings → Meta data shows the meta title and description
   - Post settings → Code injection contains the VideoObject JSON-LD
5. Run `explainer run <url>` again. It should report the existing draft and do nothing.
6. Run `explainer status` to see the job as `draft_created`.
7. Without Ghost: leave `GHOST_ADMIN_API_KEY` empty, run `explainer run <url> --force`, and check that the job ends as `article_ready` and `generated_blogs/<slug>.md` has the front matter, embed, watch links and code blocks.

## Tests

```bash
.venv/bin/pytest
```

Unit tests cover URL parsing, chapter parsing, ISO 8601 durations, JWT generation, transcript chunking, HTML/Markdown/JSON-LD rendering, the quality checks (including the n-gram overlap check), code matching, updating a Ghost draft in place, and job status. They make no network calls.
