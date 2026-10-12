"""Prepare a video for description by an agent that can read images but not video.

Claude Code (and most coding agents) can look at an image with their file-read tool
but cannot open a video or hear audio. This module turns one video into what such an
agent needs, deterministically and without any model call:

  * Shots from ffmpeg scene detection (frame-exact boundaries), with long shots split
    so a static scene still gets sampled every few seconds.
  * A few frames per shot, tiled into labeled contact sheets. One sheet is one image
    read: six frames for roughly the token cost of one or two single frames.
  * Measured speech spans from the audio track, and the audio itself when there is
    speech, so an external transcriber can fill the Transcript section.
  * A manifest that binds all of the above to the exact video bytes (sha256), so
    `cdaf assemble` can refuse to write a sidecar for a video that changed meanwhile.

Everything lives in a work directory next to the video: `<dir>/.cdaf-work/<video name>/`.
"""

from __future__ import annotations

import json
import platform
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .local import (
    DEFAULT_SCENE_THRESHOLD,
    _duration,
    _require_ffmpeg,
    _run,
    _sample_times,
    _ts,
    build_shots,
    coarsen,
    detect_cuts,
    speech_spans,
)
from .sidecar import hash_file

WORK_DIR_NAME = ".cdaf-work"
MANIFEST_NAME = "manifest.json"
BODY_NAME = "body.md"
TRANSCRIPT_NAME = "transcript.txt"
AUDIO_NAME = "audio.wav"
MANIFEST_VERSION = 1

DEFAULT_TILE_WIDTH = 512
DEFAULT_COLS = 3
DEFAULT_ROWS = 2

# Frames sampled per shot, and the longest span one shot may cover before it is split
# into equal parts, by --detail level. Splitting matters for footage without cuts
# (interviews, screen recordings): scene detection finds nothing, yet the content moves.
DETAIL_FRAMES = {"brief": 1, "standard": 2, "rich": 3}
DETAIL_MAX_SHOT = {"brief": 20.0, "standard": 10.0, "rich": 6.0}

# Fontconfig is not available in every ffmpeg build (it crashes on Windows), so labels
# use an explicit font file. Without any of these the sheets are unlabeled and the
# manifest alone maps tiles to shots.
_FONT_CANDIDATES = {
    "Windows": ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/arial.ttf"],
    "Darwin": ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf",
               "/Library/Fonts/Arial.ttf"],
    "Linux": ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
              "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
              "/usr/share/fonts/dejavu/DejaVuSansMono.ttf"],
}


class PrepareError(RuntimeError):
    pass


@dataclass
class Frame:
    t: float
    sheet: str
    tile: int


@dataclass
class Shot:
    n: int
    start: float
    end: float
    frames: list[Frame] = field(default_factory=list)

    @property
    def span(self) -> str:
        return f"[{_ts(self.start)}-{_ts(self.end)}]"


@dataclass
class Sheet:
    file: str
    cols: int
    rows: int
    tiles: list[dict] = field(default_factory=list)   # {"tile", "shot", "t"}


@dataclass
class Manifest:
    video: str
    sha256: str
    bytes: int
    duration: float
    detail: str
    scene_threshold: float
    max_shot: float
    shots: list[Shot]
    sheets: list[Sheet]
    speech: list[list[float]]
    audio: str | None
    labeled: bool
    version: int = MANIFEST_VERSION

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Manifest":
        raw = json.loads(text)
        if raw.get("version") != MANIFEST_VERSION:
            raise PrepareError(f"unsupported manifest version: {raw.get('version')}")
        shots = [Shot(n=s["n"], start=s["start"], end=s["end"],
                      frames=[Frame(**f) for f in s["frames"]]) for s in raw["shots"]]
        sheets = [Sheet(**s) for s in raw["sheets"]]
        return cls(video=raw["video"], sha256=raw["sha256"], bytes=raw["bytes"],
                   duration=raw["duration"], detail=raw["detail"],
                   scene_threshold=raw["scene_threshold"], max_shot=raw["max_shot"],
                   shots=shots, sheets=sheets, speech=raw["speech"],
                   audio=raw.get("audio"), labeled=raw.get("labeled", False))


# ---------------------------------------------------------------------- paths

def work_dir_for(video: str | Path) -> Path:
    video = Path(video)
    return video.parent / WORK_DIR_NAME / video.name


def load_manifest(work: Path) -> Manifest:
    path = work / MANIFEST_NAME
    if not path.is_file():
        raise PrepareError(f"no manifest at {path}; run `cdaf prepare` first")
    return Manifest.from_json(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------- shots

def split_long(shots: list[tuple[float, float]],
               max_len: float) -> list[tuple[float, float]]:
    """Split spans longer than max_len into equal parts no longer than max_len."""
    out: list[tuple[float, float]] = []
    for start, end in shots:
        span = end - start
        parts = max(1, int(-(-span // max_len)))      # ceil without math import
        step = span / parts
        for i in range(parts):
            a = round(start + step * i, 2)
            b = end if i == parts - 1 else round(start + step * (i + 1), 2)
            out.append((a, b))
    return out


def plan_shots(duration: float, cuts: list[float], detail: str) -> list[Shot]:
    spans = build_shots(duration, cuts)
    if detail == "brief":
        spans = coarsen(spans)
    spans = split_long(spans, DETAIL_MAX_SHOT[detail])
    return [Shot(n=i, start=a, end=b) for i, (a, b) in enumerate(spans, 1)]


def layout_sheets(shots: list[Shot], per_shot: int, cols: int,
                  rows: int) -> list[Sheet]:
    """Assign (shot, sample time) pairs to sheet tiles in order; fills the manifest."""
    capacity = cols * rows
    samples = [(shot, t) for shot in shots
               for t in _sample_times(shot.start, shot.end, per_shot)]
    sheets: list[Sheet] = []
    for i in range(0, len(samples), capacity):
        chunk = samples[i:i + capacity]
        sheet = Sheet(file=f"sheet-{len(sheets) + 1:02d}.jpg", cols=cols,
                      rows=max(1, -(-len(chunk) // cols)))
        for tile, (shot, t) in enumerate(chunk, 1):
            t = round(t, 2)
            sheet.tiles.append({"tile": tile, "shot": shot.n, "t": t})
            shot.frames.append(Frame(t=t, sheet=sheet.file, tile=tile))
        sheets.append(sheet)
    return sheets


# --------------------------------------------------------------------- ffmpeg

def _font_file() -> str | None:
    for candidate in _FONT_CANDIDATES.get(platform.system(), []):
        if Path(candidate).is_file():
            return candidate
    return None


def _drawtext(font: str, label: str) -> str:
    # drawtext treats ':' as an option separator and '\' as escape, in both the
    # filter string and the font path, so both are escaped.
    font = font.replace("\\", "/").replace(":", "\\:")
    label = label.replace("\\", "\\\\").replace(":", "\\:").replace("'", "")
    return (f"drawtext=fontfile='{font}':text='{label}':x=6:y=6:fontsize=22:"
            f"fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=4")


def _grab(video: Path, t: float, dest: Path, width: int, label: str | None,
          font: str | None) -> bool:
    """One frame-accurate grab, scaled to the tile width, optionally labeled.

    -ss after -i decodes to the exact time instead of snapping to a keyframe.
    """
    filters = [f"scale={width}:-2"]
    if label and font:
        filters.append(_drawtext(font, label))
    try:
        _run(["ffmpeg", "-v", "error", "-i", str(video), "-ss", f"{t:.3f}",
              "-frames:v", "1", "-vf", ",".join(filters), "-q:v", "3",
              str(dest), "-y"], timeout=120)
    except (subprocess.SubprocessError, OSError):
        return False
    return dest.exists() and dest.stat().st_size > 0


def _tile(frame_files: list[Path], cols: int, rows: int, dest: Path) -> None:
    """Compose a sheet from frame files via ffmpeg's tile filter."""
    listing = dest.with_suffix(".txt")
    listing.write_text(
        "".join(f"file '{f.resolve().as_posix()}'\nduration 1\n" for f in frame_files),
        encoding="utf-8")
    try:
        _run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
              "-vf", f"tile={cols}x{rows}:padding=6:margin=6:color=0x202020",
              "-frames:v", "1", "-q:v", "3", str(dest), "-y"], timeout=300)
    except (subprocess.SubprocessError, OSError) as e:
        raise PrepareError(f"could not compose {dest.name}: {e}") from e
    finally:
        listing.unlink(missing_ok=True)


def _extract_audio(video: Path, dest: Path) -> bool:
    try:
        _run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1",
              "-ar", "16000", str(dest), "-y"], timeout=300)
    except (subprocess.SubprocessError, OSError):
        return False
    return dest.exists() and dest.stat().st_size > 0


# -------------------------------------------------------------------- prepare

def prepare(
    video: str | Path,
    *,
    detail: str = "standard",
    scene_threshold: float = DEFAULT_SCENE_THRESHOLD,
    tile_width: int = DEFAULT_TILE_WIDTH,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    work: Path | None = None,
) -> tuple[Path, Manifest]:
    """Build the work directory for `video` and return (work_dir, manifest)."""
    if detail not in DETAIL_FRAMES:
        raise ValueError(f"detail must be one of {sorted(DETAIL_FRAMES)}")
    _require_ffmpeg()
    video = Path(video)
    if not video.is_file():
        raise PrepareError(f"no such video: {video}")
    work = work or work_dir_for(video)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    duration = _duration(video)
    shots = plan_shots(duration, detect_cuts(video, scene_threshold), detail)
    sheets = layout_sheets(shots, DETAIL_FRAMES[detail], cols, rows)
    font = _font_file()

    frames_dir = work / "frames"
    frames_dir.mkdir()
    for sheet in sheets:
        files: list[Path] = []
        for tile in sheet.tiles:
            t = min(tile["t"], duration - 0.05)
            dest = frames_dir / f"{sheet.file[:-4]}-{tile['tile']:02d}.jpg"
            label = f"S{tile['shot']} {_ts(tile['t'])}"
            if not _grab(video, t, dest, tile_width, label, font):
                raise PrepareError(f"could not extract a frame at {t:.2f}s from {video.name}")
            files.append(dest)
        _tile(files, sheet.cols, sheet.rows, work / sheet.file)
    shutil.rmtree(frames_dir, ignore_errors=True)

    speech = [[round(a, 2), round(b, 2)] for a, b in speech_spans(video)]
    audio = AUDIO_NAME if speech and _extract_audio(video, work / AUDIO_NAME) else None

    manifest = Manifest(
        video=video.name, sha256=hash_file(video), bytes=video.stat().st_size,
        duration=round(duration, 3), detail=detail, scene_threshold=scene_threshold,
        max_shot=DETAIL_MAX_SHOT[detail], shots=shots, sheets=sheets, speech=speech,
        audio=audio, labeled=font is not None,
    )
    (work / MANIFEST_NAME).write_text(manifest.to_json(), encoding="utf-8")
    return work, manifest


def describe_work(work: Path, manifest: Manifest) -> str:
    """Human/agent-readable index of a work directory: what to read, what to write."""
    lines = [f"video: {manifest.video}  ({manifest.duration:.1f}s, {len(manifest.shots)} shots, "
             f"{len(manifest.sheets)} contact sheets, detail={manifest.detail})",
             f"work:  {work}", ""]
    lines.append("Contact sheets (read each one; tiles run left-to-right, top-to-bottom):")
    for sheet in manifest.sheets:
        tiles = ", ".join(f"{t['tile']}=S{t['shot']}@{_ts(t['t'])}" for t in sheet.tiles)
        lines.append(f"  {sheet.file}  {sheet.cols}x{sheet.rows}  {tiles}")
    lines += ["", "Shots (one Segments line per shot, these exact timestamps):"]
    for shot in manifest.shots:
        lines.append(f"  S{shot.n} {shot.span}")
    lines.append("")
    if manifest.speech:
        spans = ", ".join(f"{_ts(a)}-{_ts(b)}" for a, b in manifest.speech)
        lines.append(f"Speech measured at: {spans}")
        lines.append(f"  audio for transcription: {work / manifest.audio}" if manifest.audio
                     else "  (audio extraction failed)")
        lines.append(f"  write a transcript to {work / TRANSCRIPT_NAME} as '[MM:SS.d] text' "
                     "lines, or leave it out and the spans are recorded as untranscribed")
    else:
        lines.append("Speech: none measured (Transcript will be '(no speech)')")
    lines += ["", f"Then write {work / BODY_NAME} and run: cdaf assemble \"{manifest.video}\""]
    return "\n".join(lines)
