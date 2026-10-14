# Screenwriter Studio

An AI-assisted pre-production desk for short films intended for social media.
Turn an idea into a developed story, a reviewed screenplay, a timed shot list,
reference-linked character, location, and keyframe stills, and a final generated video.

After still approval, generate animated shots with native audio and assemble a
downloadable MP4. Captions, separate music/voice mixing, and publishing remain future stages.

## Workflow

```text
Brief → Development → Screenplay + shot list → Script review → Art plan → Stills → Image inspection → Final Video
                              ↑                     │
                              └── up to two rewrites┘
```

Python controls the sequence. Each creative agent returns a structured result,
and the script must pass both editorial review and deterministic checks before
image generation starts. A failed draft stays failed; the studio never deletes
shots to manufacture an approval.

The local browser desk provides a brief editor, live crew progress, still and
video viewers, an activity log, and a library of saved production documents and media.

## Quick start

Requires **Python 3.12** on **macOS or Linux**, plus a KIE.ai API key. Node.js is
needed only for the Markdown-renderer test; the desk has no frontend build step.

Run these commands from this repository's root:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp -n .env.example .env
```

Set `KIE_API_KEY` in `.env`, then open the desk:

```bash
python -m screenwriter_studio --ui
```

Open the printed `http://127.0.0.1:…` address on the same computer. The desk is
local-only and does not create a public tunnel.

## Run a production

A 30-second vertical short with a maximum of six shots:

```bash
python -m screenwriter_studio \
  "A night-shift cleaner tries to return a violin before the last train leaves." \
  --runtime 30 --aspect 9:16 --max-shots 6
```

| Option | Behaviour |
| --- | --- |
| `--ui` | Open the local browser desk. |
| `--runtime 30` | Lock the total duration in seconds; supported range is 6–720. |
| `--aspect 9:16` | Lock `9:16`, `16:9`, or `1:1`. |
| `--max-shots 6` | Set the maximum shot/keyframe count. Also available in the desk. |
| `--inspection manual` | Inspect images and approve proposed repairs yourself (default). Use `auto` for up to two repairs per image. |
| `--pages-only` | Stop after screenplay review, before art planning and stills. |
| `--dry-run` | Develop and review the film, then save a still plan without generating images. |

The default shot limit is one per eight seconds, adjusted when necessary to make
the runtime feasible. It is a generation-count limit, not a fixed editing rhythm
or a monetary budget. Character and location references are additional images.
Shots can last 3–15 seconds and must add up exactly to the locked runtime.

**Chat and image generation are paid calls.** Both `--pages-only` and the studio's
`--dry-run` still use paid chat models. To check an existing still plan without
any model calls, use the standalone image command described below.

The original `python run.py …` and `python generate_stills.py …` commands remain
available as compatibility launchers.

## Image accuracy review

Choose **Manual** or **Auto** in the brief form before starting a production.
Both inspect the actual generated image against the brief, locked development,
shot list, image prompt, and supplied reference images. Checks cover action,
identity, wardrobe, props, composition, continuity and obvious visual defects.
Minor cosmetic imperfections should pass; this is an accuracy check, not a
promise of perfect images.

- **Manual (default):** usable images pass. Others pause in the Stills tab with
  findings, proposed changes, details to preserve, and expandable exact repair
  instructions. View the full image, then choose **Approve these changes**,
  **Keep this image**, or **Inspect again**. No repair starts without approval.
- **Auto:** clear defects trigger targeted repairs, each followed by inspection.
  Uncertainty, inspection errors, or the two-repair limit pause for human review.

Both modes allow at most two repair versions per image. Unapproved references
block dependent shots. Originals and repair versions stay on disk; the manifest
records findings, decisions, task IDs and history. **Keep this image** explicitly
accepts the current version, even if the inspector found issues. Approving or
keeping an image also resumes eligible downstream image work.

Inspection uses paid vision calls through the existing KIE Responses client and
art model; repairs incur image-generation charges too. Provider vision support
must be available for the configured model. If unavailable, the workflow pauses
for human review. A normal image preview (`--dry-run`) never inspects images.
The request format follows the [OpenAI image-input documentation](https://developers.openai.com/api/docs/guides/images-vision);
KIE compatibility is tested with mocked responses, not live paid calls.

After interruption, use **Resume saved image work** in Stills. This continues the
current production, without rerunning the screenplay. Legacy jobs without an
`inspection` setting keep their existing generation behavior. Modes are saved
in the plan; changing the brief selector only affects new productions.

## Generate the final video

Open **Final Video** after the stills are complete and approved. Every screenplay
shot needs its own keyframe (`s01`, `s02`, etc.). Productions with a smaller image
budget than their shot count cannot generate a complete video until all keyframes exist.

Leave **Model selection** on **Auto** to choose a suitable model for each shot,
or require **Kling 3.0** or **Seedance 2.5** for the production. Choose **Standard
(720p)**, **Pro (1080p, default)**, or **4K**, and whether to generate dialogue and
sound from the shot notes. **Save video plan · free** writes an offline
plan without uploads or model calls. **Generate final video** makes additional paid
KIE calls, one per shot; higher quality and audio affect cost. It is a separate,
explicit step and never starts automatically after still generation.

Final assembly requires `ffmpeg` and `ffprobe` on PATH. On macOS, install them with
`brew install ffmpeg`; on Debian/Ubuntu, use `sudo apt install ffmpeg`. Restart the
desk after installation. Missing tools are reported before paid video submissions.

The adapters use the official [KIE Kling 3.0](https://docs.kie.ai/market/kling/kling-3-0)
and [KIE Seedance 2.5](https://docs.kie.ai/market/bytedance/seedance-2-5) contracts,
checked on 2026-10-14. Kling accepts 3–15 seconds and includes 4K; Seedance accepts
4–30 seconds and up to 1080p on this endpoint. The screenplay still uses 3–15 second
shots. Each approved keyframe remains the first frame; Seedance's reference-image
mode is not mixed into first-frame requests. Local keyframes use the existing KIE
file store.

Prompts follow the official [Kling user guide](https://kling.ai/quickstart/klingai-video-3-model-user-guide)
and [Seedance prompt guide](https://docs.volcengine.com/docs/ark/seedance-2-5-prompt-guide?lang=en):
Kling gets cinematic action/camera prose and attributed speech; Seedance gets a
first-frame role, a summary, a continuous shot timeline, and continuity instructions.
The original action, audio, language, and notes are preserved. Templates do not
invent unattached reference assets or translate dialogue.

Auto first enforces timing, format, native resolution, and documented speech-language
support. Among compatible models, the studio prefers Kling for dialogue and short
single-subject shots; Seedance for visual action lasting at least ten seconds,
multiple named subjects, compound camera movement, or instructions too long for
Kling's prompt limit. These preferences are explicit studio heuristics, not measured
quality rankings or vendor rules. Shot cards expose the model, reason, exact prompt,
and official source links before generation. Unsupported combinations fail before
paid submission instead of silently changing quality, duration, or spoken language.

Native dialogue, lip sync and voice consistency depend on the model; there is no
separate dialogue recording or audio inspection stage.

FFmpeg normalizes clips to H.264/AAC at 24 fps and assembles them in shot order.
Silent clips receive a silent audio track so cuts retain their timing. The result
is `videos/final.mp4`, playable and downloadable in Final Video and available in
the library alongside the individual clips.

From the command line:

```bash
# Offline plan, no credits or FFmpeg required:
python -m screenwriter_studio.videos ../productions/last-train --dry-run

# Generate, or resume the same saved production:
python -m screenwriter_studio.videos ../productions/last-train

# Choose settings on the first run (saved settings are reused thereafter):
python -m screenwriter_studio.videos ../productions/last-train --mode std --no-sound

# Render a single shot first (up to three clips run in parallel otherwise):
python -m screenwriter_studio.videos ../productions/last-train --shot s02

# Regenerate one finished shot (paid); the old take is kept as s02_v1.mp4:
python -m screenwriter_studio.videos ../productions/last-train --redo s02

# Require Seedance for all compatible shots:
python -m screenwriter_studio.videos ../productions/last-train --model seedance --mode pro
```

Model selection is offline and spends no extra chat credits. The screenplay,
keyframe content, and settings are fingerprinted. Changed inputs
are rejected when resuming; create a new production for a different version.
Saved model choices and prompts remain frozen across routing/template updates;
original version-1 Kling productions continue using their original requests.
Stop or interruption preserves saved task IDs, downloaded clips, and results.
Resume retries explicitly failed provider jobs, which may cost credits, but does
not automatically resubmit an uncertain submission. See
[video recovery](docs/recovery.md#resume-final-video) for details.

## Saved work

Productions stay in `../productions/`, beside this checkout. This preserves the
location used by earlier versions. For example:

```text
workspace/
├── screenwriter_studio/          # this repository
└── productions/
    └── last-train/
        ├── 00_development.json
        ├── drafts/              # each screenplay draft and review verdict
        ├── 01_screenplay.json    # structured screenplay and shots
        ├── 01_script.fountain
        ├── 02_shots.md
        ├── 03_still_plan.json
        ├── verdict.json
        ├── videos/
        │   ├── jobs.json        # locked video prompts and keyframe fingerprints
        │   ├── manifest.json    # provider task IDs, clip states, recovery history
        │   ├── s01.mp4          # original generated clip
        │   └── final.mp4        # assembled film
        └── images/
            ├── jobs.json
            ├── manifest.json    # saved task IDs, progress, results, and errors
            ├── manifest.preview.json  # created by image dry-run only
            └── s01.png
```

Later stages create their files only when reached. Keyframes use shot IDs such as
`s01` and `s02`; reference dependencies determine their generation order.
Incomplete coverage, duplicate IDs, and missing or circular references are
rejected before image submission.

The desk's session snapshot lives in `.state/desk_status.json` and is ignored by
Git. An existing root-level `desk_status.json` can still be read; the next save
uses `.state/`. Production files are not moved during this reorganisation.

## Preview or resume stills

Set the folder to an existing production:

```bash
images=../productions/last-train/images
```

Preview its saved image plan without spending credits:

```bash
python -m screenwriter_studio.images.generator "$images/jobs.json" \
  --out "$images" --dry-run
```

Resume an interrupted or partially failed image batch:

```bash
python -m screenwriter_studio.images.generator "$images/jobs.json" \
  --out "$images" --resume
```

Resume reuses completed images, polls saved tasks, and retries downloads from
saved result URLs. Explicitly failed provider jobs may be submitted again and
incur new charges. Script and art stages do not yet resume automatically.

See [recovery and troubleshooting](docs/recovery.md) for changed plans, legacy
manifests, cancellation, and uncertain submissions.

## Project layout

```text
screenwriter_studio/
├── __main__.py          # python -m screenwriter_studio
├── cli.py               # command-line options and output
├── studio.py            # workflow orchestration and editorial loop
├── schemas.py           # structured creative handoffs
├── budget.py            # shot and still count policies
├── events.py            # typed workflow events
├── paths.py             # shared workspace and session locations
├── models.py            # KIE clients, credentials, and model routing
├── playbooks.py         # writing guidance by production type
├── crew/                # development, writer, editor, and art agents
├── images/
│   ├── catalog.py       # image capabilities and request constraints
│   ├── service.py       # plan validation and workflow adapter
│   ├── inspection.py    # visual verdicts against actual images and requirements
│   ├── review.py        # approval, reference gates and bounded versioned repairs
│   └── generator.py     # submission, polling, checkpoints, and downloads
├── videos/
│   ├── catalog.py       # official capabilities and documented routing preferences
│   ├── prompts.py       # separate Kling and Seedance prompt templates
│   ├── plan.py          # approved keyframes and deterministic video prompts
│   ├── service.py       # resumable provider tasks, downloads, and checkpoints
│   ├── assembly.py      # FFmpeg normalization and final MP4 assembly
│   └── web.py           # public summaries and seekable video delivery
└── web/
    ├── server.py        # local HTTP routes and event streaming
    ├── board.py         # desk state and persistence
    ├── brief.py         # story suggestions
    └── static/          # desk.html, desk.css, and desk.js

tests/                   # offline regressions and shared fixtures
docs/                    # operational guidance
run.py                   # compatibility launcher
generate_stills.py       # compatibility launcher
requirements.txt         # tested dependency versions
```

Use package-qualified imports throughout the application. The crew defines
creative behaviour; `studio.py` owns execution order. The image generator owns
provider jobs and recovery. The web layer renders structured events rather than
inferring state from activity text. Model IDs live in `models.py` and image
capabilities in `images/catalog.py`.

## Development checks

With the virtual environment activated, run from the repository root:

```bash
python -B -m unittest discover -s tests -t . -v
node --check screenwriter_studio/web/static/desk.js
```

Tests mock the generation providers and spend no credits. With FFmpeg installed,
the video integration test assembles synthetic clips and checks order, audio,
dimensions, and duration; it skips when the tools are unavailable. HTTP tests use a
temporary session directory and an ephemeral loopback port. No KIE key is
required to run the suite.
