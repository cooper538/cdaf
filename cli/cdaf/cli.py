"""cdaf — command-line tool for CDAF sidecars.

Commands:
  generate   Create/refresh .cdaf sidecars (Gemini, a local model, or Claude Code)
  prepare    Make contact sheets + manifest so an agent can describe a video itself
  assemble   Validate an agent-written body and write the sidecar
  validate   Verify a sidecar is well-formed and fresh (hash matches video)
  read       Print a sidecar's body (what an agent should consume)
  status     Report fresh/stale/missing across a directory tree
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .sidecar import (
    VIDEO_EXTENSIONS,
    SidecarError,
    check_freshness,
    load,
    save,
    sidecar_path_for,
    video_path_for,
)


def _iter_videos(paths: list[str]) -> list[Path]:
    videos: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            videos.extend(
                f for f in sorted(p.rglob("*"))
                if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
            )
        elif p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS:
            videos.append(p)
        elif p.is_file() and p.suffix.lower() == ".cdaf":
            video = video_path_for(p)
            if video:
                videos.append(video)
            else:
                print(f"warning: no video found for sidecar {p}", file=sys.stderr)
        else:
            print(f"warning: skipping {p} (not a video file or directory)", file=sys.stderr)
    return videos


def _sidecar_state(video: Path) -> str:
    """'fresh' | 'stale' | 'missing' | 'invalid' for a video's sidecar."""
    sidecar = sidecar_path_for(video)
    if not sidecar.is_file():
        return "missing"
    try:
        sc = load(sidecar)
    except SidecarError:
        return "invalid"
    return check_freshness(video, sc)


def cmd_generate(args: argparse.Namespace) -> int:
    from .generate import GenerationError, generate_sidecar  # lazy: needs google-genai

    videos = _iter_videos(args.paths)
    if not videos:
        print("no video files found", file=sys.stderr)
        return 1

    if args.provider == "claude":
        from .claude import effective_config_dir
        print(f"  claude profile  {effective_config_dir(args.config_dir)}")

    failures = 0
    for video in videos:
        sidecar = sidecar_path_for(video)
        if not args.force and _sidecar_state(video) == "fresh":
            print(f"  fresh   {video}  (skipped; use --force to regenerate)")
            continue
        print(f"  generating  {video} ...", flush=True)
        try:
            sc = generate_sidecar(
                video, model=args.model, detail=args.detail, api_key=args.api_key,
                provider=args.provider, base_url=args.base_url,
                scene_threshold=args.scene_threshold, effort=args.effort,
                config_dir=args.config_dir
            )
            save(sc, sidecar)
            print(f"  wrote   {sidecar}")
        except (GenerationError, OSError) as e:
            failures += 1
            print(f"  FAILED  {video}: {e}", file=sys.stderr)
    return 1 if failures else 0


def cmd_prepare(args: argparse.Namespace) -> int:
    from .prepare import PrepareError, describe_work, prepare

    videos = _iter_videos(args.paths)
    if not videos:
        print("no video files found", file=sys.stderr)
        return 1
    failures = 0
    for video in videos:
        if not args.force and _sidecar_state(video) == "fresh":
            print(f"  fresh   {video}  (skipped; use --force to prepare anyway)")
            continue
        try:
            work, manifest = prepare(
                video, detail=args.detail, scene_threshold=args.scene_threshold,
                tile_width=args.tile_width, cols=args.cols, rows=args.rows)
        except (PrepareError, OSError) as e:
            failures += 1
            print(f"  FAILED  {video}: {e}", file=sys.stderr)
            continue
        print(describe_work(work, manifest))
        print()
    return 1 if failures else 0


def cmd_assemble(args: argparse.Namespace) -> int:
    from .assemble import AssembleError, assemble
    from .prepare import PrepareError

    video = Path(args.path)
    if video.suffix.lower() == ".cdaf":
        found = video_path_for(video)
        if not found:
            print(f"error: no video found for sidecar {video}", file=sys.stderr)
            return 2
        video = found
    body = Path(args.body) if args.body else None
    try:
        sc = assemble(video, body_path=body, keep=args.keep)
    except PrepareError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except AssembleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3
    sidecar = sidecar_path_for(video)
    save(sc, sidecar)
    print(f"wrote   {sidecar}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    path = Path(args.path)
    sidecar_file = path if path.suffix.lower() == ".cdaf" else sidecar_path_for(path)
    if not sidecar_file.is_file():
        print(f"MISSING  no sidecar at {sidecar_file}")
        return 2
    try:
        sc = load(sidecar_file)
    except SidecarError as e:
        print(f"INVALID  {sidecar_file}: {e}")
        return 3
    video = video_path_for(sidecar_file, sc.video)
    if not video:
        print(f"ORPHAN   {sidecar_file}: paired video '{sc.video}' not found")
        return 4
    state = check_freshness(video, sc, fast=args.fast)
    print(f"{state.upper():<8} {sidecar_file}  <->  {video.name}")
    return 0 if state == "fresh" else 5


def cmd_read(args: argparse.Namespace) -> int:
    path = Path(args.path)
    sidecar_file = path if path.suffix.lower() == ".cdaf" else sidecar_path_for(path)
    if not sidecar_file.is_file():
        print(f"error: no sidecar at {sidecar_file}", file=sys.stderr)
        return 2
    try:
        sc = load(sidecar_file)
    except SidecarError as e:
        print(f"error: invalid sidecar: {e}", file=sys.stderr)
        return 3
    if not args.no_verify:
        video = video_path_for(sidecar_file, sc.video)
        if video and check_freshness(video, sc) == "stale":
            print(
                f"error: sidecar is STALE — {video.name} changed since it was written. "
                "Regenerate with: cdaf generate --force",
                file=sys.stderr,
            )
            return 5
    print(sc.body)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    videos = _iter_videos([args.path])
    if not videos:
        print("no video files found", file=sys.stderr)
        return 1
    counts = {"fresh": 0, "stale": 0, "missing": 0, "invalid": 0}
    for video in videos:
        state = _sidecar_state(video)
        counts[state] += 1
        print(f"  {state.upper():<8} {video}")
    total = len(videos)
    print(
        f"\n{total} video(s): {counts['fresh']} fresh, {counts['stale']} stale, "
        f"{counts['missing']} missing, {counts['invalid']} invalid"
    )
    return 0 if counts["fresh"] == total else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cdaf",
        description="CDAF — Cached Descriptive Asset Files: timestamped descriptive "
        "sidecars for video, so AI agents stop re-analyzing the same footage.",
    )
    parser.add_argument("--version", action="version", version=f"cdaf {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="create/refresh sidecars for videos")
    g.add_argument("paths", nargs="+", help="video files, sidecars, or directories (recursive)")
    g.add_argument("--model", default=None, help="Gemini model id (default: env CDAF_MODEL or gemini-2.5-flash)")
    g.add_argument("--detail", choices=["brief", "standard", "rich"], default="standard")
    g.add_argument("--force", action="store_true", help="regenerate even if sidecar is fresh")
    g.add_argument("--api-key", default=None, help="Gemini API key (default: env GEMINI_API_KEY)")
    g.add_argument("--provider", choices=["gemini", "local", "claude"], default=None,
                   help="gemini = Files API, needs a key (default); "
                        "local = OpenAI-compatible endpoint, no key or cost; "
                        "claude = Claude Code in print mode, no key, subscription "
                        "(default: env CDAF_PROVIDER)")
    g.add_argument("--local", dest="provider", action="store_const", const="local",
                   help="shorthand for --provider local")
    g.add_argument("--claude", dest="provider", action="store_const", const="claude",
                   help="shorthand for --provider claude")
    g.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"],
                   default=None, help="claude: effort level passed to claude -p "
                                      "(default: env CDAF_CLAUDE_EFFORT)")
    g.add_argument("--config-dir", default=None, metavar="DIR",
                   help="claude: Claude Code profile to run under, sets CLAUDE_CONFIG_DIR "
                        "(default: env CDAF_CLAUDE_CONFIG_DIR, else the caller's "
                        "CLAUDE_CONFIG_DIR, else ~/.claude)")
    g.add_argument("--scene-threshold", type=float, default=None, metavar="F",
                   help="local: ffmpeg scene-detect sensitivity, 0-1 (default 0.15). "
                        "Lower finds more cuts, including low-contrast ones, at the "
                        "cost of splitting on camera movement")
    g.add_argument("--base-url", default=None,
                   help="local endpoint (default: env CDAF_BASE_URL or "
                        "http://127.0.0.1:8090/v1)")
    g.set_defaults(func=cmd_generate)

    p = sub.add_parser("prepare", help="contact sheets + manifest for an agent to describe")
    p.add_argument("paths", nargs="+", help="video files or directories (recursive)")
    p.add_argument("--detail", choices=["brief", "standard", "rich"], default="standard")
    p.add_argument("--force", action="store_true", help="prepare even if the sidecar is fresh")
    p.add_argument("--scene-threshold", type=float, default=None, metavar="F",
                   help="ffmpeg scene-detect sensitivity, 0-1 (default 0.15)")
    p.add_argument("--tile-width", type=int, default=None, metavar="PX",
                   help="width of one frame in a sheet (default 512)")
    p.add_argument("--cols", type=int, default=None, help="frames per sheet row (default 3)")
    p.add_argument("--rows", type=int, default=None, help="rows per sheet (default 2)")
    p.set_defaults(func=cmd_prepare)

    a = sub.add_parser("assemble", help="validate an agent-written body and write the sidecar")
    a.add_argument("path", help="a video file or a .cdaf path")
    a.add_argument("--body", default=None, help="body markdown (default: <work>/body.md)")
    a.add_argument("--keep", action="store_true", help="keep the work directory")
    a.set_defaults(func=cmd_assemble)

    v = sub.add_parser("validate", help="check one sidecar is well-formed and fresh")
    v.add_argument("path", help="a video file or a .cdaf file")
    v.add_argument("--fast", action="store_true", help="size check only, skip hashing")
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("read", help="print a sidecar's body (verifies freshness first)")
    r.add_argument("path", help="a video file or a .cdaf file")
    r.add_argument("--no-verify", action="store_true", help="print without hashing the video")
    r.set_defaults(func=cmd_read)

    s = sub.add_parser("status", help="fresh/stale/missing report for a directory tree")
    s.add_argument("path", nargs="?", default=".", help="directory to scan (default: .)")
    s.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    if args.command == "generate":
        from .generate import DEFAULT_PROVIDER
        if getattr(args, "provider", None) is None:
            args.provider = DEFAULT_PROVIDER
        if getattr(args, "model", None) is None:
            # Each provider has its own default model id.
            if args.provider == "local":
                from .local import DEFAULT_LOCAL_MODEL
                args.model = DEFAULT_LOCAL_MODEL
            elif args.provider == "claude":
                from .claude import DEFAULT_CLAUDE_MODEL
                args.model = DEFAULT_CLAUDE_MODEL
            else:
                from .generate import DEFAULT_MODEL
                args.model = DEFAULT_MODEL
        if getattr(args, "config_dir", None) is None:
            from .claude import DEFAULT_CLAUDE_CONFIG_DIR
            args.config_dir = DEFAULT_CLAUDE_CONFIG_DIR
    if args.command == "prepare":
        from .local import DEFAULT_SCENE_THRESHOLD
        from .prepare import DEFAULT_COLS, DEFAULT_ROWS, DEFAULT_TILE_WIDTH
        defaults = {"scene_threshold": DEFAULT_SCENE_THRESHOLD, "tile_width": DEFAULT_TILE_WIDTH,
                    "cols": DEFAULT_COLS, "rows": DEFAULT_ROWS}
        for key, value in defaults.items():
            if getattr(args, key) is None:
                setattr(args, key, value)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
