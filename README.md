# Screenwriter Studio

An AI-assisted desk for short films intended for social media. An idea becomes a
developed story, a reviewed screenplay with a shot list, one generated image per shot
(from prompts written by the art director), and one animated video clip per image.

The clips are the end product: download them and combine them in your own editor.
The studio does not merge clips, add captions, mix music or publish.

## Workflow

```text
Idea → Development → Screenwriter → Script editor → Your approval → Image prompts → Images → Image inspection → Video clips
                              ↑                     │
                              └── up to two rewrites┘
```

Python controls the sequence. Each creative agent returns a structured result,
and the script must pass both editorial review and deterministic checks before
image generation starts. A failed draft stays failed; the studio never deletes
shots to manufacture an approval.

A shot is one video clip: every shot gets one image, and every image is animated into
that shot's clip. The left rail lists your **Productions** with a picture, how far each got,
and how many images wait for you; it collapses to pictures only. The app bar names the open
production, its length and format, and an estimate of the image and clip credits it has used.
**＋ New production** describes a new film (the brief and options). The open production has
three numbered steps: **1 Story** (development, screenwriter, script editor, your approval,
image prompts), **2 Images** (generation, inspection, approval) and **3 Video clips** (one
animated clip per image); a tick appears on a step once it is finished. **Activity** and
**Files** open a drawer beside the work with the log, the saved files, and a viewer for any
of them. One status bar under the steps says what happens next, in amber when it needs you.
Keys: `N` new production, `1`–`3` steps, `A` activity, `F` files, `Esc` closes the drawer.

In **Story**, the steps run down the left and the selected step's output sits on the right;
when the pages wait for you, an approval card above it shows what approving makes and costs.
In **Images**, everything that needs a decision is reviewed one at a time in a panel that puts
the image beside the reference pictures it should match, with **‹ Prev / Next ›** between them.
In **Video clips**, the clips form a strip whose card widths follow the shot lengths, and
**▶ Play in order** previews the cut without merging any files. On **New production**,
**＋ Add reference images** opens a picker with search.

The local browser desk provides a brief editor, live crew progress, still and
video viewers, an activity log, and a library of saved production documents and media.

## Quick start

Requires **Python 3.12 or newer** (tested on 3.13) on **macOS or Linux**, plus a KIE.ai
API key. Node.js is optional: two desk-script tests use it and skip without it. The desk
has no frontend build step. FFmpeg is optional too: with `ffprobe` installed the studio
checks image proportions and clip lengths.

Run these commands from this repository's root:

```bash
python3 -m venv .venv
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

## Idea tab

The Idea tab holds this film's choices first. The saved model choices sit in one closed
**Models and quality** panel at the bottom, whose heading summarises them; they are saved in
`.state/studio_settings.json` and used by every production. **Reset models** inside the panel
restores them without clearing the idea, the film, or the references. The rough cost sits
beside **Start production**.

- **Idea:** write the story, or press **Suggest a story**. With reference images selected
  it becomes **Suggest a story from N references**: the Development model gets, as text,
  each reference's role, name, direction and the prompt its image was generated from (read
  from that production's saved image plan, including edited takes, and followed back to the
  source for copied references) and writes a brief around them. The pictures themselves are
  not sent; requests carrying image data failed at KIE's gateway with 502 errors. An
  uploaded picture has no saved prompt, so only its role, name and file name are used. Character references become the cast, Location
  references the setting, an Object / Product reference the thing the story turns on,
  Style the tone and genre, and Composition one moment's framing. Names and extra
  directions you gave a reference are used. With text already in the box, the button
  strengthens your note instead and keeps your idea. It is one small paid chat call (up to
  eight references) and generates no images.
- **Film:** length, format, and maximum shots for this production.
- **Approval:** wait, continue, or stop after the script.
- **Images:** how many images to generate, inspection, prompt-only planning, and reference
  images.
- **Models and quality** (saved, closed by default):
  - *Images:* the image models, resolution, and images made at once. Shot, character and
    location images need reference-image support. Nano Banana 2.1 and Nano Banana Pro are the
    choices there.
  - *Video:* Auto, Kling 3.0 or Seedance 2.5; quality (the list follows the chosen model,
    and 4K is Kling only); sound; and how many clips are generated at once (1–4).
  - *Crew:* the chat model for development, the screenwriter, the script editor, the image
    prompts and the image inspector. GPT-6 Astra, GPT-6.1 Sol, GPT-6 Sol, and GPT-6 Luna. All
    four accept images and strict JSON.

  Each setting shows one line with its price at the current choices. **Model details and
  sources** lists every model in use once, with its notes and documentation links.

An image the studio already knows (a character or location sheet, or a saved reference)
starts in that role with its name; anything else starts as **Style** (colour, light, texture
and mood). Change the role to **Character**, **Location**, **Object / Product**, or
**Composition** on each selected image. Character, location and object references show their
name on the card and are flagged until named. Each card also sets where the reference applies (automatically, or specific shot numbers
such as `1, 3-5`), an extra direction, and shows **What the image model is told**. Earlier versions of an image are
hidden behind **Show earlier versions**, the selection survives a reload, and **Use the
references from …** restores the open production's saved choices. Character references have separate **Face** and **Wardrobe** switches.
Use the script's exact character or location name for automatic matching; other subjects and
composition references are assigned to relevant shots by the art director. Automatic style
references apply to all shot and location images. Specific-shot references apply only to those
shot images, never to shared character or location sheets. Missing shot numbers and reference
limits stop image generation with an explanation instead of silently dropping a selection.
These settings are saved with the production and used in image prompts, inspection and repairs.
Existing productions using **Lock** keep their previous meaning. Role-specific image bindings
follow Google's [multi-image prompting guidance](https://ai.google.dev/gemini-api/docs/image-generation#advanced_composition_combining_multiple_images);
the five roles and automatic matching are studio controls, not extra provider API fields.

Every chat role (the writing roles, image prompts and the image inspector) also has a
**reasoning effort**: model default, low, medium or high, shown with **Show reasoning effort**
in Models and quality (and listed in its heading when any is changed). Model default sends no
reasoning field. Low is already used on this endpoint; medium and high follow the OpenAI Responses
enum, but KIE does not publish its accepted values, so a rejected value shows up as a run
error. Higher effort is slower and costs more.

Only models with a checked KIE request format are listed (`images/catalog.py`,
`videos/catalog.py`, `models.py`). To offer another KIE model, add its verified card to the
matching catalog and list it in `settings.py`; the desk draws the settings from that
registry. A production keeps the video settings it started with once clips exist.

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
| `--no-approval` | Continue straight into paid image work when the editor passes the pages. By default the run pauses with the pages. |
| `--proceed FOLDER` | Continue a saved production into image prompts and images, including a draft the editor did not pass. Paths are relative to where you run the command. |

Before any image prompt or image is paid for, the studio checks that every shot can
become a clip with the current video settings: shot lengths and the runtime, a video
model for each shot at the chosen quality, sound and dialogue language, and an image
budget of one image per shot plus character and location sheets. If not, it stops and
says why.

Each shot in the shot list carries structured fields next to its framing, action
and audio: `characters` (the locked names on screen), `location` (one locked
location), `camera_move` (the one move during the take, or static) and `dialogue`
(each spoken line with its speaker). The script editor sends a draft back when a
name or place does not match development, a shot has more than one speaker, or
its lines run past about 2.5 words per second. These notes never block paid work.
The fields drive the rest of the pipeline: every shot image gets the sheet of each
listed character attached, the inspector checks that exactly those people appear,
the video router counts the listed cast, and clip prompts attribute each line to
its speaker. Screenplays saved before these fields existed still open and render.

## Cost estimates

Prices come from KIE's public [price list](https://kie.ai/pricing), checked on
2026-10-14 and kept in `screenwriter_studio/pricing.py`; KIE bills what its page
says on the day, so treat every figure as a guide. One credit is $0.005.

| Model | Credits |
| --- | --- |
| Kling 3.0 | per second: 720p 20 with sound, 14 silent · 1080p 27 / 18 · 4K 67 |
| Seedance 2.5 | per second: 720p 63 · 1080p 158 (no separate sound price) |
| Nano Banana 2.1 | per image: 1K 8 · 2K 12 · 4K 18 (KIE lists Nano Banana 2; that price is used) |
| Nano Banana Pro | per image: 1K 18 · 2K 18 · 4K 24 |
| GPT Image Sunburst (and its edit) | per image: 1K 6 · 2K 10 · 4K 16 |
| Seedream 5 Pro | per image: 1K 7 · 2K 14 |

The desk shows an estimate before each paid step: a rough range on the Idea tab,
the images and clips a script will lead to beside **Approve pages**, the unfinished
images on **Resume saved image work**, the price of a proposed repair, and the
price of every clip, of the unfinished ones, and of a single render or redo in
the Video clips tab and its confirmation. The CLI prints the same estimate when
the pages wait for approval and with `python -m screenwriter_studio.videos … --dry-run`.
Chat calls (story, image prompts, inspections) are billed per token and are not
included. Auto can route a long or multi-subject shot to Seedance, which costs
several times more per second than Kling; choose Kling in **Models and quality**
to cap that.

The default shot limit is one per eight seconds, adjusted when necessary to make
the runtime feasible. It is a generation-count limit, not a fixed editing rhythm
or a monetary budget. Character and location references are additional images.
Shots last 3–15 seconds on Kling, 4–30 seconds on Seedance 2.5, and 3–30 seconds on Auto. They must add up exactly to the locked runtime. A 30-second Seedance clip can be the whole film.

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

- **Manual (default):** usable images pass. Others pause in the Images tab with
  findings, proposed changes, details to preserve, and expandable exact repair
  instructions. View the full image, then choose **Approve these changes**,
  **Abort** (keep the current image), or **Inspect again**. No repair starts without approval.
- **Auto:** clear defects trigger targeted repairs, each followed by inspection.
  Uncertainty, inspection errors, or the two-repair limit pause for human review.

Both modes allow at most two repair versions per image. Unapproved references
block dependent shots. Originals and repair versions stay on disk; the manifest
records findings, decisions, task IDs and history. **Abort** explicitly
accepts the current version, even if the inspector found issues. Approving or
keeping an image also resumes eligible downstream image work.

Inspection uses paid vision calls through the existing KIE Responses client and
art model; repairs incur image-generation charges too. Provider vision support
must be available for the configured model. If unavailable, the workflow pauses
for human review. A normal image preview (`--dry-run`) never inspects images.
The request format follows the [OpenAI image-input documentation](https://developers.openai.com/api/docs/guides/images-vision);
KIE compatibility is tested with mocked responses, not live paid calls. When KIE answers an
inspection with 429 or a 5xx error, it is retried twice before the image waits for review; a
timeout is not retried, because the first request may already have been billed.

2K and 4K stills can exceed KIE's 8 MB upload. The copy sent to KIE (as a reference, repair
source or clip first frame) then drops an opaque alpha channel, which keeps every pixel, or
becomes a high-quality JPEG if that is still too large. The file on disk never changes.

Images are generated and inspected side by side, 1–4 at a time (**Images generated
at once** in **Models and quality**; default 3). A shot image starts as soon as the
character and location images it uses are done. An image waiting for your review
holds back only the images that use it; the rest carry on.

After interruption, use **Resume saved image work** in the Images tab. This continues the
current production, without rerunning the screenplay, even while some images wait
for review. Legacy jobs without an `inspection` setting keep their existing generation
behavior. Modes are saved in the plan; changing the brief selector only affects new productions.

### Edit, regenerate and choose versions

Every finished, failed or waiting image card has **Edit prompt and regenerate**. A new
take is made from your prompt (paid, about one image plus its inspection), saved as
`s01.take-2.png`, inspected against the edited prompt, and becomes the image. Only that
image is generated; images that use it are not redone. The original, every repair and
every take stay on disk; **Earlier versions** on the card switches back to any of them
without a provider call. Clips use whichever version is current when they are rendered.

From the command line:

```bash
images=../productions/last-train/images
python -m screenwriter_studio.images.generator "$images/jobs.json" --out "$images" \
  --regenerate s01 --prompt "Wide shot, low angle, Maya runs through the hall."
python -m screenwriter_studio.images.generator "$images/jobs.json" --out "$images" \
  --use-version s01=s01.png
```

## Generate the video clips

Open the **Video clips** tab after the images are complete and approved. Every screenplay
shot needs its own keyframe (`s01`, `s02`, etc.). With sound on and FFmpeg installed, each finished
clip's loudness is measured; a clip that averages below −45 dB is kept but its card says it
plays as near silence. Productions with a smaller image
budget than their shot count cannot generate a complete video until all keyframes exist.

Leave **Video model** on **Auto** to choose a suitable model for each shot,
or require **Kling 3.0** or **Seedance 2.5** for the production. Choose **Standard
(720p)**, **Pro (1080p, default)**, or **4K**, and whether to generate dialogue and
sound from the shot notes. **Preview clip plan · free** writes an offline
plan without uploads or model calls. Under **Choose clips to animate**, tick the shots
you want and press **Animate selected**; each shows its estimated credits, and the
other shots wait until you pick them. **Animate all clips** (later **Animate all
remaining clips**) does every shot that is not finished yet. Each clip is a paid KIE
call; higher quality and audio affect cost. Animating is a separate, explicit step
and never starts automatically after still generation.

`ffprobe` (part of FFmpeg) is optional. When it is on PATH the studio checks keyframe
proportions and clip durations; without it those checks are skipped. Clips are never
re-encoded or merged. Several clips are generated at the same time (1–4, set in
**Models and quality**; default 3).

The adapters use the official [KIE Kling 3.0](https://docs.kie.ai/market/kling/kling-3-0)
and [KIE Seedance 2.5](https://docs.kie.ai/market/bytedance/seedance-2-5) contracts,
checked on 2026-10-14. Kling accepts 3–15 seconds and includes 4K; Seedance accepts
4–30 seconds and up to 1080p on this endpoint. The shot list uses that clip length,
so Seedance can hold one clip for up to 30 seconds. Each approved keyframe remains the first frame; Seedance's reference-image
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

Each clip is saved untouched as `videos/s01.mp4`, `videos/s02.mp4` and so on, playable
and downloadable from its card in the Video clips tab and listed in the library. If a
clip is not right, **Redo this shot** regenerates just that one (paid). The previous
take is kept as `s02_v1.mp4` and stays downloadable. **Edit prompt** on a clip card
redoes (or renders) that shot with your own prompt; the planned prompt is kept beside
it in `videos/jobs.json`, and editing one shot never locks the others. **Use this take**
next to an earlier take makes it the clip again and keeps the replaced one as an
earlier take, with no provider call. On the command line: `--prompt s02="…"` with
`--redo s02` or `--shot s02`, and `--use-take s02=s02_v1.mp4`.

From the command line:

```bash
# Offline plan, no credits required:
python -m screenwriter_studio.videos ../productions/last-train --dry-run

# Generate, or resume the same saved production:
python -m screenwriter_studio.videos ../productions/last-train

# Choose settings on the first run (saved settings are reused thereafter):
python -m screenwriter_studio.videos ../productions/last-train --mode std --no-sound

# Render a single shot first (otherwise clips run in parallel, as set in the desk):
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
[video recovery](docs/recovery.md#resume-video-clips) for details.

## Open an earlier production

Select a production in the **Productions** rail (or **Open** in Files). It puts that production back on
the desk with its steps, images and clips, so you can approve its script, review or resume
images, or render and redo clips. The activity log of the original run is not kept.
Opening is refused while a run is in progress.

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
        ├── run_settings.json    # the idea, image count and references this run used
        ├── greenlight.json      # written when you continue a draft
        ├── aborted.json         # written when you abort a draft
        ├── references/          # copies of the reference images you chose
        ├── videos/
        │   ├── jobs.json        # locked video prompts and keyframe fingerprints
        │   ├── manifest.json    # provider task IDs, clip states, recovery history
        │   ├── jobs.preview.json  # the free clip plan from --dry-run
        │   ├── s01.mp4          # original generated clip
        │   ├── s02_v1.mp4       # earlier take, kept after a redo
        │   └── s03_rejected.mp4 # a clip that failed its length check, kept to view
        └── images/
            ├── jobs.json
            ├── manifest.json    # saved task IDs, progress, results, and errors
            ├── manifest.preview.json  # created by image dry-run only
            ├── s01.png
            └── lena.repair-1.png  # a repaired version; the original stays
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
├── pricing.py           # KIE credit prices and estimates before paid steps
├── settings.py          # saved model and quality choices
├── errors.py            # one-line, path-free error text for the desk
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
│   ├── probe.py         # optional ffprobe checks on keyframes and clips
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

Tests mock the generation providers and spend no credits. The suite refuses any
network connection other than loopback, so a missed mock fails instead of calling
KIE. HTTP tests use a temporary session directory and an ephemeral loopback port, and
settings go to a temporary file. No KIE key is required to run the suite.
