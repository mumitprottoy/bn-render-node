# Render node contract

The interfaces between the parts of this repo. The plan behind it: `bn_v2/docs/studio/render-node-plan.md` (sections
9 to 15) and `bn_v2/docs/studio/library-labels-and-search.md`. Where this file and those differ, this file wins for
code.

## 1. Folders on the node

Root `BN_RENDER_ROOT` (Windows: `D:\bn-render`; tests: a temporary folder).

| Path | What |
|---|---|
| `app/` | this repo, a git clone of `main` |
| `tools/blender/` | Blender 5.2.3 portable |
| `venv/` | the Python environment |
| `assets/` | scene assets fetched from `sources/assets/` (textures, HDRI) |
| `models/` | models a job needs (pinned URL, SHA-256, licence) |
| `work/<job id>/<item slug>/` | an item in progress; deleted once the item is registered |
| `state/runner.db` | SQLite: jobs, items, progress, speeds, uploads, events |
| `state/paused` | present means paused (survives reboots) |
| `state/log.txt` | rotating log |
| `.env` | `BN_API_KEY=...` (the only secret), optional `BN_API_BASE` |

## 2. Job files: `library/jobs/<job id>.yaml`

```yaml
id: plates-league-night            # unique, lowercase, hyphens
title: 3D plates, league night
kind: plate_3d                     # a kind from the taxonomy
device: gpu                        # gpu (one at a time) or cpu (pool)
priority: 10                       # lower runs first
after: [physics-searches]          # jobs that must be done first (optional)
inputs: [sources/photos/]          # sources/ folders that must hold files (optional)
models: []                         # model ids from library/models.yaml (optional)
executor: blender_item             # blender_item or python
entry: render_item.py              # blender_item: script in blender/; python: module:function
items:                             # or `generator: module:function` returning the same list
  - asset_id: plate_3d/strike/pocket-strike-pin-cam-slow5-league-night
    title: Pocket strike, pin cam, slow motion
    group: Strike plates
    description: The ball rolls straight at us into the 1-3 pocket; the rack is soft in the foreground.
    labels: { ... }                # facets from library/taxonomy.yaml, validated before the job runs
    params: { template: strike_pin_cam, ... }
    estimate: { frames: 210 }      # for ETA before any speed is measured (frames, or seconds for cpu items)
```

A job's state: `waiting` (with a reason), `queued`, `running`, `done`, `failed` (with a reason). A job is ready when
every `after` job is done, every `inputs` folder has files, and every model is installed and verified.

## 3. Executors

### blender_item

```
<blender> -b --factory-startup -P app/blender/<entry> -- --item <work>/item.json --out <work>/out --device HIP|METAL|CPU --samples 32 --assets <root>/assets
```

- `item.json` is the item from the job file plus `job_id`.
- Renders frames to `out/frames/0001.png ...`, **skipping frames already present** (resume). Writes each frame to a
  temporary name and renames it, so a frame on disk is always complete.
- Prints one line per finished frame: `PROGRESS <done> <total>`; at the end writes `out/meta.json` and prints `DONE`.
- Exit code 0 only on success.
- A physics simulation (strike shots) is baked once to `out/sim/` and reused when the item resumes, so a pause or a
  power cut never re-runs it.

### Pausing an item mid-render

- `bn pause` lets the frame in progress finish, then the runner ends the Blender process. Finished frames stay in
  `out/frames/`, and the item stays `running` in the state database with its frame count.
- `bn pause` twice ends Blender at once. The unfinished frame never reaches its final name, so it is redone.
- On resume, or after a power cut, the runner starts the same item again. The script skips the frames on disk and
  continues at the next one. The cost is the scene load, under half a minute.

### python

`python -m bnrender.exec <module:function> --item <work>/item.json --out <work>/out`. The function writes its files into
`out/` and returns (or writes `out/meta.json`) `{ "files": [{ "role": ..., "path": ... }], "measured": {...} }`.

## 4. `out/meta.json` for a 3D plate

```json
{
  "fps": 30, "width": 1296, "height": 2304,
  "safe_crop": { "x": 108, "y": 192, "w": 1080, "h": 1920 },
  "frame_count": 210,
  "markers": { "contact": 64, "pins_settled": 140 },
  "subjects_required": ["ball"],
  "frames": [ { "camera": { "location": [x, y, z], "rotation_quat": [w, x, y, z], "lens": 40, "shift": [sx, sy] },
                "boxes": { "ball": [x0, y0, x1, y1], "rack": [...], "monitor_glass": [[x, y], [x, y], [x, y], [x, y]] } } ],
  "measured": { "duration_s": 7.0 }
}
```

Boxes are in plate pixels (the full 1296 x 2304 frame); `safe_crop` is the centred 1080 x 1920 window that frames the shot
as designed.

## 5. Finishing a plate (runner)

From `out/frames/` and `out/meta.json`, the runner makes:

| Role | File | How |
|---|---|---|
| `video` | `video.mp4` | H.264, CRF 12, yuv420p, bt709 tags, 30 fps, faststart |
| `frames` | `frames.json` | `meta.json` as is |
| `preview` | `preview.mp4` | the safe crop at 540 x 960, CRF 28 |
| `sheet` | `sheet.jpg` | contact sheet, one frame per second of the safe crop |

Checks before upload (a failure marks the item failed with its reason; nothing is uploaded):
- frame count equals `frame_count`, and every PNG decodes;
- exposure: mean luminance of the safe crop between 0.06 and 0.85, clipped highlights under 2% of pixels, on every 10th frame;
- every subject in `subjects_required` is visible in the safe crop on every frame: its box overlaps the safe crop
  (a box may extend past the frame, as near pins do in the pin cam; a `null` box means not visible).

## 6. Uploading and registering

The API is `BN_API_BASE` (default `https://backend.bowlersnetwork.com/api/studio/library`). **Every request,
including the PUT to R2, sends `User-Agent: bn-render-node/1.0`** (Cloudflare blocks Python's default agent with
error 1010) and, to the API, `X-API-Key`.

1. `POST /uploads` `{asset_id, kind, files: [{role, filename, size, sha256}]}` returns `{files: [{role, key, url, headers}]}`.
2. `PUT url` with exactly `headers` and the file body, for each file.
3. `POST /register` `{asset_id, kind, title, description, group, labels, measured, files: [{role, filename, size, sha256}], job_fingerprint, taxonomy_version}`.
   It returns 200 with the record once every file is verified in R2.
4. Only then delete `work/<job id>/<item slug>/`.

- `GET /assets/<asset_id>` returns 200 if registered: the runner skips such an item when the job fingerprint matches.
- `GET /sources?prefix=` lists inputs with download links (paginated with `next`).
- Network errors and 5xx: retry with backoff, up to 30 minutes, without stopping other work.
- 4xx: the item fails with the API's message.

`job_fingerprint` is the SHA-256 of the canonical JSON of the item (labels and params included) plus the template version.

## 7. Commands (PowerShell, thin wrappers over `python -m bnrender`)

- `bn status`: progress overall and per job (done, total, left, speed, ETA), the unit in progress, library uploads, disk, GPU, last error.
- `bn pause`: finish the unit in progress, then stay paused (flag file). Run twice to stop the unit now.
- `bn resume`: end a pause.

Automatic (no command): start at boot unless paused; every 15 minutes between units, pull `main` (fast-forward only),
self-check, and roll back on failure; list `sources/`; start jobs whose requirements are met. When every job is done:
confirm every item is registered, delete `work/`, remove `.env`'s key, status FINISHED.

## 8. Priority and limits

Job processes run at "above normal" priority (never high or realtime). CPU jobs use up to 11 of 12 threads and about 13
GB of RAM in total. One GPU job at a time. A GPU out-of-memory failure is retried (up to 3 times), then the item fails.

## Runner notes (how `bnrender` reads the points above)

- **Fingerprint:** SHA-256 of the item's canonical JSON (sorted keys, no spaces; `estimate` left out because it changes
  nothing made), a newline, and the template version. The template version is the item's or job's `template_version`;
  for a `blender_item` job without one, the `TEMPLATE_VERSION` constant in `blender/` (the entry script first); else `1`.
- **Optional job keys:** `threads` and `ram_gb` (per item, for the CPU budget; defaults 1 and 1 GB, or 4 and 4 GB for
  Blender), `samples`, `unit_name` (the noun in `bn status`), `template_version`, `repeat: daily`, and a job-level
  `estimate` (`frames`, `seconds_per_frame` or `seconds`) used for items without their own.
- **Item inputs:** an item may list `sources: [sources/...]`; those files are fetched into `<work>/inputs/` and their
  paths given in item.json as `input_files`. Scene assets (`sources/assets/`) are mirrored into `<root>/assets/` before
  any `blender_item` job runs, kept until the node finishes, and never uploaded.
- **python executor:** the function is called as `function(item: dict, out_dir: Path)`; a `generator` as
  `generator(job: dict, sources: {folder: [{key, size, url}]})` and returns the item list.
- **Units and pausing:** a unit is one frame for `blender_item` and one item for `python`. A first pause ends Blender
  right after its next `PROGRESS` line; a python item runs to its end.
- **Failures:** out of graphics memory is retried 3 times; any other executor failure once. Failed items are tried again
  after a code update. Network errors and 5xx on upload retry with backoff for 30 minutes; the item is then marked failed
  and queued again at the next 15-minute check (its files stay on disk).
- **Exposure:** luminance uses Rec. 709 weights on the frame's sRGB values; a pixel counts as a clipped highlight when
  its darkest channel is at least 250 of 255.
- **`repeat: daily`:** never skipped as already registered. After a run the job waits; after 06:00 local time it runs
  again if any other item was registered since its last run. When every other job is done it runs once more if behind,
  and the node finishes only when its last run is current.
- **Self-update:** `git fetch` then `git merge --ff-only` (the same as `pull --ff-only`), so a version that failed its
  self-check is not tried again until `main` moves on. A change to `bnrender/` or `catalogue/` code restarts the runner
  after the units in progress.
- **Finish:** also deletes `<root>/assets/`; the space freed counts both folders.
