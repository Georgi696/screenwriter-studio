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

From this repository's root, install the tested dependencies with Python 3.12:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Set `KIE_API_KEY` in the environment or in `.env` (see `.env.example`). Then:

```bash
.venv/bin/python run.py "a 30-second vertical short about a dented thermos" --runtime 30 --aspect 9:16 --max-shots 6
.venv/bin/python run.py "idea" --pages-only
.venv/bin/python run.py "idea" --dry-run
.venv/bin/python run.py --ui
```

The desk binds only to `127.0.0.1`; it no longer opens an automatic public tunnel.
Open the printed address on the same computer. Cross-origin requests and untrusted
Host headers are rejected. Remote use requires a separately authenticated gateway.

`--dry-run` plans stills but **still makes paid chat calls**. `--pages-only` also
uses paid chat. To preview an already saved still plan without any model calls:

```bash
.venv/bin/python generate_stills.py ../productions/<slug>/images/jobs.json --out ../productions/<slug>/images --dry-run
```

## Production gates

- The default keyframe limit is one per eight seconds, increased where necessary
  to make the requested runtime feasible. `--max-shots` (also in the desk) overrides
  it. This is a count limit, not a currency budget or fixed pacing rule.
- Shots are 3–15 seconds; their durations must total the locked runtime exactly.
  Shot numbers must be unique and sequential. These checks run in Python in
  addition to the editorial review.
- After two unsuccessful rewrites, the draft stays rejected. Shots are never
  silently removed and a rejected draft is never automatically approved.
- Every screenplay draft and editorial verdict is saved. The art plan is saved
  before validation; invalid plans spend no image credits. Keyframes must cover
  every shot, with IDs `s01`, `s02`, etc. Dependencies must exist and be acyclic.
- The desk consumes structured events, so activity wording cannot change state.
  A partially failed image batch is reported as failed, not complete.

## Output and recovery

Productions retain the existing layout: `productions/` is a sibling of this
checkout (for example `../productions/` when running the commands above).

```text
productions/<slug>/
├── 00_development.json
├── drafts/                 # numbered screenplays and verdicts
├── 01_screenplay.json       # structured shots, preserved for later editing
├── 01_script.fountain
├── 02_shots.md
├── 03_still_plan.json
├── verdict.json
└── images/
    ├── jobs.json
    ├── manifest.json        # incrementally saved task IDs and results
    ├── manifest.preview.json # created by dry-run only
    └── s01.png
```

Resume an interrupted or partially failed **still batch**:

```bash
.venv/bin/python generate_stills.py ../productions/<slug>/images/jobs.json --out ../productions/<slug>/images --resume
```

Completed images are reused. Saved tasks are polled without resubmission;
failed downloads reuse their saved result URL. Explicit provider failures can
be submitted again by `--resume`, which may incur new charges. A changed plan
requires a new output folder; legacy manifests without a plan fingerprint
are not automatically resumed. The folder is locked while generating to prevent
concurrent duplicate batches (macOS/Linux).

If a submission response was lost before its task ID was recorded, resume stops
that job rather than risking a duplicate charge. Check the provider's job history:
if the job exists, record its `task_id` and set its manifest state to `polling`;
remove that job's manifest entry only if the provider confirms no job was created.
Leave the plan fingerprint unchanged.

Cancelling the desk prevents further queued work once cancellation is observed.
Already submitted provider jobs may continue and remain billable; their saved
IDs can be resumed. Restarting the desk marks its old active run interrupted.
Script/art stages do not yet resume automatically; their saved drafts and plans
remain available for inspection.

## Tests

```bash
.venv/bin/python -B -m unittest discover -v
```

Tests use mocked providers and spend no generation credits. Desk tests bind an
ephemeral loopback port; the Markdown-preview test also requires Node.js.
