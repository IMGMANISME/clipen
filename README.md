# clipen

AI-assisted vlog auto-editing for raw video footage. Point clipen at a
folder of raw clips, bind your own LLM API token (Claude, Codex/OpenAI,
or a free local model via Ollama), and get back an editable timeline +
captions — not a locked, unchangeable video.

Two ideas drive everything here:

- **Tweaks never re-invoke the AI.** `edit` writes an
  [OpenTimelineIO](https://opentimeline.io/) timeline and an SRT caption
  file. Hand-edit either one and re-run `render` — it's a pure, fast
  ffmpeg step that never touches the LLM.
- **Local analysis is free; the one AI call is honest about its cost.**
  Speech-to-text, shot detection, and silence detection all run locally,
  at zero token cost. Deciding which clips to keep, what order tells a
  good story, and roughly which part of each clip is worth keeping is a
  single AI call per edit — its prompt size scales with how much footage
  you throw at it, and the result is cached so re-running never re-spends
  tokens on unchanged input.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Requires `ffmpeg`/`ffprobe` on your `PATH`. Run `clipen doctor` to check
your environment.

## Quick start

```bash
clipen auth add claude              # bind a provider once (anthropic | openai-compatible)
clipen project init ./my-footage    # auto-detects mp4/mov/mxf/avi/mkv in the folder
clipen project analyze ./my-footage # local only: speech-to-text + shots + silence, no tokens spent
clipen project edit ./my-footage --instructions "energetic, fast cuts"
clipen project render ./my-footage  # writes .clipen/render/output.mp4
```

Not happy with a cut or a caption? Edit `.clipen/timeline.otio` or
`.clipen/captions.srt` directly and run `clipen project render` again —
no AI call, no re-analysis, just ffmpeg.

## Optional: let it actually look at your footage

By default, `edit` decides using only transcripts and audio level — the
same signal a human editor gets from reading a shot log. If you also want
it to notice how a clip actually looks (composition, shakiness, "this is
just generic B-roll"), bind a second, local vision model and pass it in:

```bash
# needs Ollama running locally with a vision-capable model pulled, e.g.:
#   ollama pull qwen2.5vl
clipen auth add ollama-vlm   # kind=openai-compatible, base_url=http://localhost:11434/v1, model=qwen2.5vl
clipen project edit ./my-footage --vision-provider ollama-vlm
```

This adds one extra (typically free, local) call per kept clip. The one
paid, cloud call stays text-only either way — the local model's job is
just to turn pixels into a sentence before the cloud model ever sees the
project.

## Status

Early rebuild, vlog-editing pipeline only. No take-based interview
editing, no reusable style profiles, and no cloud footage import yet.

## Development

```bash
pip install -e ".[dev]"
pytest
```
