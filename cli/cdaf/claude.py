"""Generate a CDAF body with Claude Code on a subscription: no API key, no per-token bill.

`claude -p` (print mode) runs one headless turn of Claude Code under the user's own
login, so a Pro/Max subscription covers it. Claude Code reads images with its Read
tool but cannot open video, so this provider hands it the contact sheets that
`cdaf prepare` made, asks for structured output, and assembles the body itself.

Only Read is enabled and the session is not persisted. `--bare` is deliberately not
used: it skips keychain reads, so the headless run would start logged out.

Profile: Claude Code keeps login, settings and any API proxy per config directory
(`CLAUDE_CONFIG_DIR`). The headless run uses the directory of the session or shell it
was started from, so it is billed to the same account. `config_dir` /
CDAF_CLAUDE_CONFIG_DIR overrides that explicitly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from .assemble import build_body, read_transcript, transcript_section
from .generate import GenerationError
from .local import DEFAULT_SCENE_THRESHOLD, _parse_json, _ts
from .prepare import DEFAULT_COLS, DEFAULT_ROWS, DEFAULT_TILE_WIDTH, Manifest, prepare

DEFAULT_CLAUDE_MODEL = os.environ.get("CDAF_CLAUDE_MODEL")      # None = the user's default
DEFAULT_CLAUDE_EFFORT = os.environ.get("CDAF_CLAUDE_EFFORT")
DEFAULT_CLAUDE_CONFIG_DIR = os.environ.get("CDAF_CLAUDE_CONFIG_DIR")
CLAUDE_TIMEOUT = 1800

_DETAIL_SENTENCES = {"brief": "one short sentence", "standard": "1-2 sentences",
                     "rich": "2-4 sentences covering subjects, actions, setting, "
                             "camera framing and movement, lighting, color and mood"}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"shot": {"type": "integer"}, "description": {"type": "string"}},
                "required": ["shot", "description"],
            },
        },
        "screen_text": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "string"}, "text": {"type": "string"}},
                "required": ["t", "text"],
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "segments", "screen_text", "tags"],
}

PROMPT_RULES = """\
Rules (they protect the agents that will read this instead of watching the video):
- Describe only what is visible in the frames. Do not narrate what probably happened \
between frames or after the last one.
- Do NOT say a task was completed, fixed, or finished unless the finished result is \
plainly visible. Someone holding a tool or a detached part is intent, not completion.
- Name what you can identify (a screwdriver, a light bulb, a MacBook); fall back on \
shape and color only when you genuinely cannot tell.
- Transcribe visible text EXACTLY as written. Report deliberate, legible text only: \
burned-in titles, captions, signage, screens, handwriting. Ignore incidental text on \
packaging in the background, and never guess a brand from a partly visible logo.
- Write each description so it stands alone, then keep continuity across shots \
(introduce a person or place once, refer back afterwards).
- Do not mention the sheets, tiles, labels, or that you are looking at still frames.
- Write everything in English, whatever language other instructions or your user settings ask for: the sidecar is read by agents and tools that expect English."""


def build_prompt(work: Path, manifest: Manifest) -> str:
    sheets = "\n".join(
        f"- {work / s.file}  tiles: " +
        ", ".join(f"{t['tile']}=S{t['shot']}@{_ts(t['t'])}" for t in s.tiles)
        for s in manifest.sheets)
    shots = "\n".join(f"- S{s.n} {s.span}" for s in manifest.shots)
    layout = (f"Each sheet is a {manifest.sheets[0].cols}-column grid; tiles run "
              f"left-to-right, top-to-bottom" if manifest.sheets else "")
    labels = ("Each tile is labeled in its top-left corner with its shot number and time."
              if manifest.labeled else "Tiles are not labeled; use the tile lists below.")
    return f"""You are producing the body of a CDAF sidecar: a timestamped description of \
a video that AI agents will read INSTEAD of watching it. Describe the video \
"{manifest.video}" ({manifest.duration:.1f}s) from the contact sheets below.

{layout}. {labels} Read every sheet with the Read tool before writing anything.

Contact sheets:
{sheets}

Shots (describe every one; one entry per shot, in {_DETAIL_SENTENCES[manifest.detail]}):
{shots}

{PROMPT_RULES}

Output JSON with:
- summary: one short paragraph: what the clip is, its arc, what it would be useful for.
- segments: one object per shot, {{"shot": N, "description": "..."}}, all {len(manifest.shots)} shots in order.
- screen_text: deliberate on-screen text with the time of the frame it was seen in, \
{{"t": "MM:SS.d", "text": "exact text"}}; empty list if none.
- tags: 12-20 retrieval keywords (subjects, actions, setting, mood, camera work, lighting, genre).
"""


def claude_command(prompt_schema: dict, work: Path, *, model: str | None,
                   effort: str | None) -> list[str]:
    exe = shutil.which("claude")
    if not exe:
        raise GenerationError("`claude` (Claude Code) is not on PATH; install it or use another provider")
    cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence",
           "--tools", "Read", "--allowedTools", "Read", "--add-dir", str(work),
           "--json-schema", json.dumps(prompt_schema)]
    if model:
        cmd += ["--model", model]
    if effort:
        cmd += ["--effort", effort]
    return cmd


def claude_env(config_dir: str | None) -> dict[str, str]:
    """Environment for the headless run: the caller's, with the profile pinned when asked."""
    env = dict(os.environ)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    return env


def effective_config_dir(config_dir: str | None) -> str:
    """The config directory the headless run will use, for reporting."""
    return claude_env(config_dir).get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")


def run_claude(prompt: str, work: Path, *, model: str | None, effort: str | None,
               config_dir: str | None = None, usage_out: dict | None = None) -> dict:
    cmd = claude_command(OUTPUT_SCHEMA, work, model=model, effort=effort)
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              encoding="utf-8", timeout=CLAUDE_TIMEOUT, cwd=str(work),
                              env=claude_env(config_dir))
    except (subprocess.SubprocessError, OSError) as e:
        raise GenerationError(f"could not run claude: {e}") from e
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        detail = (proc.stderr.strip() or proc.stdout.strip())[:400]
        raise GenerationError(f"claude exited {proc.returncode} without a JSON result: {detail}") from e
    if result.get("is_error") or proc.returncode != 0:
        raise GenerationError(f"claude reported an error: {str(result.get('result'))[:400]}")
    if usage_out is not None:
        usage_out.update({
            "claude_config_dir": effective_config_dir(config_dir),
            "claude_cost_usd": result.get("total_cost_usd"),
            "claude_turns": result.get("num_turns"),
            "seconds": round(result.get("duration_ms", 0) / 1000, 2),
        })
    structured = result.get("structured_output")
    if isinstance(structured, dict):
        return structured
    try:
        return _parse_json(str(result.get("result", "")))
    except ValueError as e:
        raise GenerationError("claude did not return the structured description") from e


def body_from_output(manifest: Manifest, out: dict, work: Path) -> tuple[str, str]:
    """Structured output to (canonical body, transcript source); validates coverage."""
    by_shot: dict[int, str] = {}
    for item in out.get("segments") or []:
        try:
            by_shot[int(item["shot"])] = str(item["description"]).strip()
        except (KeyError, TypeError, ValueError):
            continue
    missing = [s.n for s in manifest.shots if not by_shot.get(s.n)]
    if missing:
        raise GenerationError(f"claude left shots undescribed: {missing}")
    segments = [f"{s.span} {by_shot[s.n]}" for s in manifest.shots]
    screen = [f'[{str(i.get("t", "")).strip()}] "{str(i.get("text", "")).strip()}"'
              for i in out.get("screen_text") or [] if str(i.get("text", "")).strip()]
    tags = [str(t).strip() for t in out.get("tags") or [] if str(t).strip()]
    transcript, source = transcript_section(manifest, read_transcript(work))
    body = build_body(manifest, summary=str(out.get("summary") or ""), segments=segments,
                      transcript=transcript, screen_text=screen, tags=tags)
    return body, source


def describe_video_claude(
    video: str | Path,
    *,
    detail: str = "standard",
    model: str | None = DEFAULT_CLAUDE_MODEL,
    effort: str | None = DEFAULT_CLAUDE_EFFORT,
    config_dir: str | None = DEFAULT_CLAUDE_CONFIG_DIR,
    scene_threshold: float | None = None,
    tile_width: int = DEFAULT_TILE_WIDTH,
    cols: int = DEFAULT_COLS,
    rows: int = DEFAULT_ROWS,
    keep: bool = False,
    usage_out: dict | None = None,
) -> tuple[str, Manifest, str]:
    """Prepare, ask Claude Code, assemble. Returns (body, manifest, transcript source)."""
    threshold = DEFAULT_SCENE_THRESHOLD if scene_threshold is None else scene_threshold
    work, manifest = prepare(video, detail=detail, scene_threshold=threshold,
                             tile_width=tile_width, cols=cols, rows=rows)
    try:
        out = run_claude(build_prompt(work, manifest), work, model=model, effort=effort,
                         config_dir=config_dir, usage_out=usage_out)
        body, source = body_from_output(manifest, out, work)
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)
            parent = work.parent
            if parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
    if usage_out is not None:
        usage_out.update({"shots": len(manifest.shots), "sheets": len(manifest.sheets)})
    return body, manifest, source
