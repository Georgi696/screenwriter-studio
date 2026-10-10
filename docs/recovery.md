# Recovery and troubleshooting

Run the commands below from the repository root with the virtual environment
activated. Replace `last-train` with your production folder.

## Resume a still batch

```bash
images=../productions/last-train/images
python -m screenwriter_studio.images.generator "$images/jobs.json" \
  --out "$images" --resume
```

The generator checkpoints each submission and result in `manifest.json`.

| Saved state | Resume behaviour |
| --- | --- |
| Completed image and local file | Reuse the image. |
| Known provider task ID | Continue polling that task without resubmission. |
| Generated result but failed download | Retry the saved result URL. |
| Explicit provider failure | Submit a retry; this may incur another charge. |
| Submission with no confirmed task ID | Stop that job for reconciliation. |

Only one batch can use an image folder at a time. A folder lock prevents
concurrent runs from duplicating submissions. A normal invocation against an
existing paid manifest requires `--resume`; it does not overwrite it silently.

## Submission outcome is unknown

A timeout can occur after the provider accepted a request but before its task ID
was saved. Automatically retrying could create a second billable job, so the
generator leaves that record in `submitting` state.

Check the provider's job history before changing the manifest:

1. If the job exists, set that entry's `task_id` and change its `state` to
   `polling`, then resume.
2. Remove the entry only if the provider confirms no job was created. Resume
   can then submit it.

Preserve the manifest's plan fingerprint. Keep a backup before editing a manifest.

## Changed plans and older productions

A manifest is tied to its saved plan by a fingerprint. If you change a prompt,
reference, ratio, or another plan input, use a new output folder. Existing images
remain available in the old folder.

Legacy manifests without fingerprints are not automatically resumed. Review
existing outputs and provider jobs before starting a fresh batch; a new folder
can incur new generation charges.

## Cancellation and restart

Cancelling the desk stops further queued work once cancellation is observed.
Already submitted provider jobs may continue and remain billable. Resume their
saved task IDs rather than launching the production again.

Restarting the desk marks an abandoned active run as interrupted. Script and art
stages do not resume automatically, but saved drafts, review verdicts, and plans
remain in the production directory.

## Common problems

- **No API key:** set `KIE_API_KEY` in the repository's `.env` and restart the
  process. The key is shared by chat and image calls.
- **Script remains rejected:** read `verdict.json` and the numbered files in
  `drafts/`. The workflow stops after two rewrites instead of deleting shots.
- **Invalid still plan:** inspect `03_still_plan.json`. It must contain one
  keyframe per shot, correct IDs, and valid references. No image submissions
  occur when plan validation fails.
- **Browser cannot connect:** use the printed localhost address on the computer
  running the desk. Automatic public tunnels are disabled; remote access needs
  a separately authenticated gateway.
- **Dry-run still incurred charges:** the studio dry-run performs paid writing
  and review calls. Only a dry-run of an already saved image plan avoids all
  model calls. It writes `manifest.preview.json`, leaving the paid manifest intact.
- **Tests cannot bind a port:** the desk tests require permission to open an
  ephemeral loopback listener. They do not call a generation provider.
