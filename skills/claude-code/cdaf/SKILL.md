---
name: cdaf
description: Read CDAF sidecar files (.cdaf) instead of processing video with vision. Use whenever a task involves understanding, summarizing, searching, editing, or selecting from video files (b-roll, raw clips, footage libraries) — check for a .cdaf sidecar FIRST before analyzing any video directly. When no sidecar exists, generate one yourself from contact sheets (`cdaf prepare` / `cdaf assemble`), no API key needed.
---

# CDAF — Cached Descriptive Asset Files

A `.cdaf` file is a timestamped, pre-computed description of a video, sitting next to
it with the same basename (`clip.mp4` → `clip.cdaf`). Reading it costs a few hundred
text tokens; analyzing the video directly costs orders of magnitude more (~263 tokens
per second of footage on Gemini-class models). **Always prefer the sidecar when it is
fresh.**

Format spec and tooling: https://github.com/UditAkhourii/cdaf

## The rule

Before analyzing ANY video file (`.mp4`, `.mov`, `.mkv`, `.webm`, `.avi`, `.m4v`):

1. **Check for a sidecar**: same directory, same basename, `.cdaf` extension.
2. **Verify freshness** before trusting it (see below). A stale sidecar describes an
   older version of the video — using it is worse than not having one.
3. **If fresh**: read the sidecar instead of processing the video. Use it as the
   account of what the video contains — within the limits below.
4. **If missing or stale**: generate one (see below) so the cost is paid once. If you
   cannot generate, fall back to direct video analysis.

## Verifying freshness

The sidecar header carries `bytes` (file size) and `sha256` (content hash) of the
exact video it describes. **Verification never needs an API key or network access.**

- **Cheap check (usually enough)**: compare the video's current file size to the
  header's `bytes` value. Different size → provably stale.
- **Strict check**: `cdaf validate <video>` (exit 0 = fresh), or hash the file
  yourself and compare to the header's `sha256`:
  - PowerShell: `(Get-FileHash clip.mp4 -Algorithm SHA256).Hash.ToLower()`
  - POSIX: `sha256sum clip.mp4` / `shasum -a 256 clip.mp4`
- Use the strict check when the decision is expensive to get wrong (publishing,
  final edits); the cheap check suffices for exploration.

## Reading a sidecar

It is plain UTF-8 text — use the Read tool directly, or `cdaf read <video>` (which
verifies the hash automatically and refuses to print a stale sidecar).

Format: a `key: value` header between `--- CDAF/1.0` and `---`, then markdown:

- `## Summary` — what the clip is
- `## Segments` — `[MM:SS.d-MM:SS.d] description` lines covering the whole video
  (see the trust note below before cutting on these timestamps)
- `## Transcript` — spoken words with timestamps (or `(no speech)`)
- `## On-screen Text` — visible text with timestamps (or `(none)`)
- `## Tags` — retrieval keywords

## How much to trust a fresh sidecar

Freshness proves the sidecar describes *these exact bytes*. It does not prove the
description is complete or correct — it is generated text, the record of one model's
pass. Three limits matter when a mistake is expensive:

- **Timestamps are approximate.** Boundaries inferred by a model drift (over a second
  on measured clips), miss real cuts, and occasionally mark cuts that do not exist.
  They are fine for locating, ranking, and rough trims. Before cutting on them, verify
  against the container:
  ```bash
  ffmpeg -v error -i clip.mp4 -vf "select='gt(scene,0.1)',metadata=print:file=-" -f null -
  ```

- **Descriptions can add, not just omit.** Shown a whole video at once, a model may
  narrate the outcome a clip implies but never shows — reporting that a task was
  completed when the footage only shows it being started. Such entries are fluent,
  specific, and indistinguishable from correct ones. Treat any claim that something
  was **finished, fixed, repaired, or achieved** as unverified: check the frames before
  relying on it, and say the sidecar is your source when you report it. An omission is
  a visible gap; an addition reads exactly like a fact.

- **Fine visual state is unreliable.** Strike-through and other mark-up on a list
  (crossed out, checked, highlighted), small or stylised text, and subtle motion are
  often missed or reported at chance. Incidental background text on packaging or
  labels has produced confident phantom brand names. If such a detail carries the
  meaning of the shot, look at the frame.

**Provenance keys.** When a producer records how the body was made, `x-` keys tell you
which parts were measured rather than inferred:

| Key | Means |
|---|---|
| `x-shot-source: ffmpeg-scene-detect@<threshold>` | Boundaries came from the container and are frame-exact — no need for the ffmpeg check above. |
| `x-shot-isolation: per-shot` | Each segment was described without sight of the others, which suppresses invented continuity between shots. |
| `x-shot-isolation: contact-sheet` | Segments were described from labeled frame grids, a few shots per image. Boundaries are measured, continuity between shots is inferred. |
| `x-shot-maxlen: <seconds>` | Shots longer than this were split into equal parts, so a boundary inside a long static shot is a sampling point, not a cut. |
| `x-transcript-source: transcript.txt` / `untranscribed` / `none` | The transcript came from an external transcriber, speech was measured but not transcribed, or no speech was measured. |
| `x-transcript-timing: measured-rms` | Transcript times were measured from audio. `none` means no measurable speech. |

Absent these keys, assume the weaker case: inferred boundaries, whole-video context,
and guessed transcript times.

None of this argues for re-watching by default — that would forfeit the entire saving.
Verify the specific claim your decision rests on, not the whole clip.

## Generating sidecars

Three providers. All write the same v1.0 format and every output passes `cdaf validate`.

### Claude Code — no API key, you describe the frames yourself

You cannot open a video, but you can read images. `cdaf prepare` turns the video into
labeled contact sheets (a grid of frames, one image per six frames) plus a manifest, and
`cdaf assemble` turns your description into the sidecar. Needs `ffmpeg` on PATH and the
`cdaf` CLI (install below). Nothing leaves the machine except what you read.

1. Run `cdaf prepare <video>`. It prints the sheets, the tile-to-shot map, the shot list,
   and the path of the work directory (`.cdaf-work/<video name>/` next to the video).
2. Read every sheet with the Read tool. Tiles run left-to-right, top-to-bottom. Each tile
   carries its shot number and time in the top-left corner.
3. Write `<work>/body.md` with these sections: `## Summary`, `## Segments`,
   `## On-screen Text`, `## Tags`. One Segments line per shot, in order, with the exact
   timestamps from the shot list: `[00:04.0-00:08.0] Description.` Use the rules below.
4. If prepare reports measured speech and you have a transcription tool, transcribe
   `<work>/audio.wav` and write `<work>/transcript.txt` as `[MM:SS.d] text` lines. If you
   cannot transcribe, skip this step. The spans are then recorded as untranscribed, which
   is honest. Never write `(no speech)` when speech was measured.
5. Run `cdaf assemble <video>`. It checks that every shot has its line with the right
   timestamps, confirms the video has not changed since prepare, writes the sidecar, and
   deletes the work directory. If it reports a problem, fix `body.md` and run it again.

Description rules (they protect every agent that reads the sidecar instead of watching):

- Describe only what is visible in the frames. Do not narrate what probably happened
  between frames or after the last one.
- Do not say a task was completed, fixed, or finished unless the finished result is
  plainly visible. Someone holding a tool or a detached part is intent, not completion.
- Name what you can identify. Fall back on shape and color only when you cannot tell.
- Transcribe visible text exactly as written. Report deliberate, legible text only.
  Ignore incidental text on background packaging, and never guess a brand from a partly
  visible logo.
- Write in English. Keep each description self-contained, then keep continuity across
  shots: introduce a person or place once and refer back to it afterwards.
- Do not mention sheets, tiles, labels, or still frames.

Options: `--detail brief|standard|rich` (1, 2 or 3 frames per shot, and a 20 s, 10 s or
6 s cap on one shot so static footage is still sampled), `--scene-threshold`, `--cols`,
`--rows`, `--tile-width`. Sheets are 1536 px wide by default, which Claude reads without
downscaling.

Batch form, from a terminal: `cdaf generate ./footage --claude`. It runs `claude -p` once
per video with only the Read tool, on the user's subscription, and assembles the result.
Inside a Claude Code session prefer the five steps above: the batch form starts a second
Claude for each video. `--model` and `--effort` pass through to `claude -p`
(or set `CDAF_CLAUDE_MODEL` / `CDAF_CLAUDE_EFFORT`). The run inherits
`CLAUDE_CONFIG_DIR`, so it uses the login, settings and proxy of the profile that
started it, and reports that directory as `claude profile`. Pin another profile with
`--config-dir <dir>` or `CDAF_CLAUDE_CONFIG_DIR`.

### Local model — no API key, no cost, footage stays on the machine

```bash
cdaf generate <video> --local          # or --provider local
```

Needs `ffmpeg` and an OpenAI-compatible endpoint serving a model with a vision encoder
(default `http://127.0.0.1:8090/v1`, override with `--base-url` / `--model`, or the
`CDAF_BASE_URL` / `CDAF_LOCAL_MODEL` env vars). An audio encoder, where the model has
one, is used for the transcript. Check the endpoint is up before offering this route:

```bash
curl -s localhost:8090/props   # llama-server: reports which modalities are loaded
```

Slower per clip than the API, but free and private, and cost scales per **shot** rather
than per second of footage — so long clips are far cheaper here. Set `CDAF_PROVIDER=local`
to make it the default.

### Gemini API

```bash
cdaf generate <video-or-directory>      # skips sidecars that are already fresh
cdaf generate <video> --force           # regenerate even if fresh
cdaf generate ./footage --detail rich   # brief | standard | rich
```

Needs Python >= 3.10 and `GEMINI_API_KEY`
(free tier: https://aistudio.google.com/apikey). Install the CLI once:

```bash
pip install "cdaf[generate] @ git+https://github.com/UditAkhourii/cdaf.git#subdirectory=cli"
```

Faster per clip and handles whole directories, but calls a paid API. **Ask the user
before batch-generating a large library**, and tell them roughly how many videos you are
about to process.

## Working across a footage library

- Survey coverage: `cdaf status <dir>` lists every video as FRESH/STALE/MISSING.
- To find footage matching a need ("sunset city shots"), grep the `.cdaf` files —
  never open the videos: search `*.cdaf` for the relevant keywords, then rank by the
  Segments detail.
- Batch-fill gaps: `cdaf generate <dir>` (fresh sidecars are skipped automatically).

## What NOT to do

- Do not treat a sidecar as fresh without at least the size check.
- Do not invent visual details beyond what the sidecar states; if the task needs
  information the sidecar lacks (exact colors, a specific frame), say so and fall
  back to targeted direct analysis of just the needed timestamp range.
- Do not edit `.cdaf` files by hand to "update" them — the header hash would then
  describe a video the body no longer matches. Regenerate with
  `cdaf generate <video> --force` instead.
