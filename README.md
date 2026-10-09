# Screenwriter studio

A crew that does what the screenwriter skill does: lock a short, write it, send it back if it fails the checklist, then generate the stills.

**Pattern.** A fixed crew, plus an editor pass before any image call. The manager in `studio.py` is code, because the order does not change from piece to piece. Chat and stills both go to KIE.ai. The only credential is `KIE_API_KEY`.

Every agent uses the same `KIE_API_KEY`. Chat models go to `POST https://api.kie.ai/codex/v1/responses`. Gemini chat models on KIE are chat-only and do not return the Agents SDK schema, so the crew uses Responses models that document `json_schema`. The model for each role is set in `models.py`.

| Agent | Model | Delivers |
|---|---|---|
| Development | `gpt-6-astra` | Logline, beats, style bible |
| Screenwriter | `gpt-6-astra` | Fountain pages, shot list |
| Art director | `gpt-6-astra` | Still prompts and a `kind` per image |
| Script editor | `gpt-6-1-sol` | Pass, or a list of fixes. Up to two rewrites |
| Stills | per image, same key | Files in `productions/<slug>/images/` |

The stills step picks the image model from `kind`: Nano Banana 2.1 (`nano-banana-2-1`) for characters, locations, and keyframes; GPT Image 2.5 Sunburst (`gpt-image-2-5-sunburst-text-to-image`) when text must be readable; Seedream 5 Pro (`seedream/5-pro-text-to-image`) for a photoreal product; GPT Image 2.5 Sunburst image-to-image (`gpt-image-2-5-sunburst-image-to-image`) for a single-image edit.

## Run

From the repo root, with `KIE_API_KEY` in the environment or in a `.env` file (see `.env.example`):

```bash
uv run screenwriter_studio/run.py "a 30-second ad for a dented thermos"
uv run screenwriter_studio/run.py "idea" --pages-only
uv run screenwriter_studio/run.py "idea" --dry-run
uv run screenwriter_studio/run.py --ui
```

Output:

```
productions/<slug>/
├── 00_development.json
├── 01_script.fountain
├── 02_shots.md
├── verdict.json
└── images/
    ├── jobs.json
    ├── manifest.json
    └── s01.png
```

## Extend

- Add a video agent after the stills pass that calls Kling on each keyframe. Keep it behind a flag so a script run does not spend video credits.
- Give the editor a second pass that only checks Fountain syntax, and leave taste to the first pass.
- When a still fails, send that job's `error` back to the art director once, then rerun just that id.
