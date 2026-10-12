"""Turn an agent-written body into a validated .cdaf sidecar.

The agent (interactive Claude Code, or `claude -p` driven by the claude provider)
describes the contact sheets that `cdaf prepare` made and writes the markdown body.
This module is the gate between that text and the sidecar on disk:

  * every shot in the manifest must have exactly one Segments line with the
    manifest's own timestamps, so boundaries stay frame-exact and contiguous;
  * the video must still hash to what the manifest recorded, otherwise the
    description would be bound to bytes it does not describe;
  * the Transcript section is filled from `transcript.txt` when an external
    transcriber produced one, or records measured speech spans as untranscribed,
    never as "(no speech)".
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .local import _ts
from .prepare import (
    BODY_NAME,
    TRANSCRIPT_NAME,
    Manifest,
    PrepareError,
    load_manifest,
    work_dir_for,
)
from .probe import probe
from .sidecar import SPEC_VERSION, Sidecar, dumps, hash_file, parse

GENERATOR = "claude-code"
TIMESTAMP_TOLERANCE = 0.06

_SEGMENT_LINE = re.compile(
    r"^\[(?P<start>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)\s*[-–]\s*"
    r"(?P<end>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)\]\s+(?P<text>\S.*)$")
_TRANSCRIPT_LINE = re.compile(r"^\[(?P<t>(?:\d{1,2}:)?\d{1,2}:\d{2}(?:\.\d+)?)\]\s+(?P<text>\S.*)$")


class AssembleError(ValueError):
    pass


# ------------------------------------------------------------------- parsing

def parse_timestamp(ts: str) -> float:
    parts = [float(p) for p in ts.split(":")]
    seconds = 0.0
    for p in parts:
        seconds = seconds * 60 + p
    return seconds


def split_sections(body: str) -> dict[str, str]:
    """`## Name` headed sections to {name: content}. Text before the first heading is dropped."""
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current, buf = line[3:].strip(), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return sections


def check_segments(manifest: Manifest, segments_text: str) -> list[str]:
    """Problems that make the Segments section unusable; empty when it matches the manifest."""
    problems: list[str] = []
    lines = [ln.strip() for ln in segments_text.split("\n") if ln.strip()]
    parsed: list[tuple[float, float, str]] = []
    for ln in lines:
        m = _SEGMENT_LINE.match(ln)
        if not m:
            problems.append(f"not a segment line: {ln[:60]!r}")
            continue
        parsed.append((parse_timestamp(m["start"]), parse_timestamp(m["end"]), m["text"]))
    if problems:
        return problems
    if len(parsed) != len(manifest.shots):
        problems.append(f"expected {len(manifest.shots)} segment lines (one per shot), "
                        f"got {len(parsed)}")
        return problems
    for shot, (start, end, _) in zip(manifest.shots, parsed):
        if abs(start - shot.start) > TIMESTAMP_TOLERANCE or abs(end - shot.end) > TIMESTAMP_TOLERANCE:
            problems.append(f"S{shot.n} must be {shot.span}, "
                            f"got [{_ts(start)}-{_ts(end)}]")
    return problems


def read_transcript(work: Path) -> list[str] | None:
    """Lines of `transcript.txt` in sidecar form, or None when the file is absent."""
    path = work / TRANSCRIPT_NAME
    if not path.is_file():
        return None
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _TRANSCRIPT_LINE.match(raw)
        if not m:
            raise AssembleError(f"{TRANSCRIPT_NAME}: expected '[MM:SS.d] text', got {raw[:60]!r}")
        out.append(f"[{_ts(parse_timestamp(m['t']))}] {m['text']}")
    return out


def transcript_section(manifest: Manifest, external: list[str] | None) -> tuple[list[str], str]:
    """(lines, x-transcript-source) for the Transcript section."""
    if external:
        return external, "transcript.txt"
    if manifest.speech:
        return ([f"[{_ts(a)}-{_ts(b)}] (untranscribed speech)" for a, b in manifest.speech],
                "untranscribed")
    return ["(no speech)"], "none"


# ------------------------------------------------------------------ building

def build_body(manifest: Manifest, *, summary: str, segments: list[str],
               transcript: list[str], screen_text: list[str], tags: list[str]) -> str:
    """Canonical body text from already-validated parts."""
    lines = ["## Summary", summary.strip() or "(unavailable)", "", "## Segments"]
    lines += segments
    lines += ["", "## Transcript"] + transcript
    lines += ["", "## On-screen Text"] + (screen_text or ["(none)"])
    lines += ["", "## Tags", ", ".join(tags) if tags else "(none)"]
    return "\n".join(lines)


def body_from_text(manifest: Manifest, text: str, external_transcript: list[str] | None) -> tuple[str, str]:
    """Validate an agent-written body and return (canonical body, transcript source)."""
    sections = split_sections(text)
    if "Segments" not in sections:
        raise AssembleError("body has no '## Segments' section")
    problems = check_segments(manifest, sections["Segments"])
    if problems:
        raise AssembleError("Segments do not match the manifest:\n  " + "\n  ".join(problems))
    segments = [ln.strip() for ln in sections["Segments"].split("\n") if ln.strip()]

    own = [ln.strip() for ln in sections.get("Transcript", "").split("\n") if ln.strip()]
    if own and own != ["(no speech)"]:
        transcript, source = own, "body"
    else:
        transcript, source = transcript_section(manifest, external_transcript)

    screen = [ln.strip() for ln in sections.get("On-screen Text", "").split("\n") if ln.strip()]
    if screen == ["(none)"]:
        screen = []
    tags = [t.strip() for t in sections.get("Tags", "").replace("\n", ",").split(",") if t.strip()]
    if tags == ["(none)"]:
        tags = []
    body = build_body(manifest, summary=sections.get("Summary", ""), segments=segments,
                      transcript=transcript, screen_text=screen, tags=tags)
    return body, source


def header_for(video: Path, manifest: Manifest, *, generator: str,
               transcript_source: str) -> dict[str, str]:
    timing = {"transcript.txt": "file", "body": "agent", "untranscribed": "measured-rms"}
    return {
        "video": video.name,
        "sha256": manifest.sha256,
        "bytes": str(manifest.bytes),
        **probe(video),
        "generator": generator,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": manifest.detail,
        "lang": "en",
        "x-shot-source": f"ffmpeg-scene-detect@{manifest.scene_threshold:g}",
        "x-shot-maxlen": f"{manifest.max_shot:g}",
        "x-shot-isolation": "contact-sheet",
        "x-transcript-source": transcript_source,
        "x-transcript-timing": timing.get(transcript_source, "none"),
    }


def assemble(video: str | Path, *, body_path: Path | None = None, work: Path | None = None,
             generator: str = GENERATOR, keep: bool = False) -> Sidecar:
    """Read the work directory, validate the body, and return the Sidecar to save.

    Raises AssembleError (fixable by the agent) or PrepareError (re-run prepare).
    """
    video = Path(video)
    work = work or work_dir_for(video)
    manifest = load_manifest(work)
    if video.stat().st_size != manifest.bytes or hash_file(video) != manifest.sha256:
        raise PrepareError(f"{video.name} changed since `cdaf prepare`; run prepare again")

    body_path = body_path or work / BODY_NAME
    if not body_path.is_file():
        raise AssembleError(f"no body at {body_path}; write it first")
    body, source = body_from_text(manifest, body_path.read_text(encoding="utf-8"),
                                  read_transcript(work))
    sc = Sidecar(version=SPEC_VERSION,
                 header=header_for(video, manifest, generator=generator, transcript_source=source),
                 body=body)
    parse(dumps(sc))                      # the sidecar must round-trip through the parser
    if not keep:
        shutil.rmtree(work, ignore_errors=True)
        parent = work.parent
        if parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
    return sc
