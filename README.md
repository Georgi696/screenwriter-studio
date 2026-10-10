# Screenwriter Studio

An AI-assisted pre-production desk for short films intended for social media.
Turn an idea into a developed story, a reviewed screenplay, a timed shot list,
and reference-linked character, location, and keyframe stills.

The current workflow ends at stills. Video generation, sound, editing, captions,
and publishing are future stages.

## Workflow

```text
Brief → Development → Screenplay + shot list → Script review → Art plan → Stills
                              ↑                     │
                              └── up to two rewrites┘
```

Python controls the sequence. Each creative agent returns a structured result,
and the script must pass both editorial review and deterministic checks before
image generation starts. A failed draft stays failed; the studio never deletes
shots to manufacture an approval.

The local browser desk provides a brief editor, live crew progress, a still
viewer, an activity log, and a library of saved production documents.

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
│   └── generator.py     # submission, polling, checkpoints, and downloads
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

Tests mock the generation providers and spend no credits. HTTP tests use a
temporary session directory and an ephemeral loopback port. No KIE key is
required to run the suite.
