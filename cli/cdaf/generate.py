"""Generate a CDAF sidecar body from a video.

Three providers: Gemini (BYOK, default), a local OpenAI-compatible endpoint
(see local.py), and Claude Code on a subscription (see claude.py). Select with the
`provider` argument or CDAF_PROVIDER.

Requires the `google-genai` package (`pip install cdaf[generate]`) and a
GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable, or an explicit key.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .probe import probe
from .sidecar import SPEC_VERSION, Sidecar, hash_file

DEFAULT_MODEL = os.environ.get("CDAF_MODEL", "gemini-2.5-flash")
DEFAULT_PROVIDER = os.environ.get("CDAF_PROVIDER", "gemini")
PROVIDERS = ("gemini", "local", "claude")

_DETAIL_GUIDANCE = {
    "brief": (
        "Keep segments coarse (5-15 seconds each) and descriptions to one short "
        "sentence. Skip the Transcript and On-screen Text sections unless speech or "
        "text is central to the clip."
    ),
    "standard": (
        "Use natural shot boundaries for segments (typically 2-8 seconds each). "
        "Describe each segment in 1-2 sentences."
    ),
    "rich": (
        "Segment at every cut or distinct beat. Describe each segment in 2-4 "
        "sentences covering subjects, actions, setting, camera framing and movement, "
        "lighting, color, and mood. Note anything an editor would cut on."
    ),
}

_PROMPT_TEMPLATE = """You are producing the body of a CDAF (Cached Descriptive Asset File): a \
timestamped description of a video that AI agents will read INSTEAD of watching the \
video. Your output must let an agent make editing and analysis decisions without ever \
seeing the footage. Describe only what is objectively visible or audible; never \
speculate or embellish.

Output GitHub-flavored markdown with EXACTLY these sections, in this order, and \
nothing else (no preamble, no code fences):

## Summary
One short paragraph: what this clip is, its overall arc, and what it would be useful for.

## Segments
Chronological, contiguous coverage from 00:00.0 to the end of the video. One line per \
segment, formatted exactly as:
[MM:SS.d-MM:SS.d] Description.
Use HH:MM:SS.d timestamps only if the video exceeds one hour. {detail_guidance}

## Transcript
Spoken words with timestamps: [MM:SS.d] Speaker: words. Label speakers (Man, Woman, \
Narrator, Speaker 1...) consistently. If there is no speech, output exactly: (no speech)

## On-screen Text
Visible text (titles, captions, signs, UI) with timestamps: [MM:SS.d] "text". If there \
is none, output exactly: (none)

## Tags
One comma-separated line of retrieval keywords: subjects, actions, setting, mood, \
camera work, lighting, genre.
"""


class GenerationError(RuntimeError):
    pass


def _client(api_key: str | None):
    try:
        from google import genai  # noqa: PLC0415 — lazy so core lib stays dependency-free
    except ImportError as e:
        raise GenerationError(
            "google-genai is not installed. Run: pip install cdaf[generate]"
        ) from e
    key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise GenerationError(
            "No API key. Set GEMINI_API_KEY (get one free at https://aistudio.google.com/apikey)."
        )
    return genai.Client(api_key=key)


def describe_video(
    video: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    detail: str = "standard",
    api_key: str | None = None,
    usage_out: dict | None = None,
) -> str:
    """Upload the video to the Gemini Files API and return the CDAF body markdown.

    If usage_out is a dict, it is filled with prompt/output token counts and
    wall-clock seconds for the generation call.
    """
    if detail not in _DETAIL_GUIDANCE:
        raise ValueError(f"detail must be one of {sorted(_DETAIL_GUIDANCE)}")
    client = _client(api_key)
    video = Path(video)

    uploaded = client.files.upload(file=str(video))
    try:
        deadline = time.monotonic() + 600
        while uploaded.state and uploaded.state.name == "PROCESSING":
            if time.monotonic() > deadline:
                raise GenerationError("timed out waiting for Gemini to process the upload")
            time.sleep(3)
            uploaded = client.files.get(name=uploaded.name)
        if uploaded.state and uploaded.state.name == "FAILED":
            raise GenerationError(f"Gemini could not process {video.name} (upload FAILED)")

        prompt = _PROMPT_TEMPLATE.format(detail_guidance=_DETAIL_GUIDANCE[detail])
        started = time.monotonic()
        response = client.models.generate_content(model=model, contents=[uploaded, prompt])
        body = (response.text or "").strip()
        if usage_out is not None:
            usage = getattr(response, "usage_metadata", None)
            usage_out.update({
                "prompt_tokens": getattr(usage, "prompt_token_count", None) if usage else None,
                "output_tokens": getattr(usage, "candidates_token_count", None) if usage else None,
                "seconds": round(time.monotonic() - started, 2),
            })
    finally:
        try:
            client.files.delete(name=uploaded.name)
        except Exception:
            pass  # best-effort cleanup; Gemini auto-expires files after 48h

    body = re.sub(r"^```(?:markdown)?\s*|\s*```$", "", body).strip()
    if "## Segments" not in body:
        raise GenerationError(
            f"model output for {video.name} lacked a '## Segments' section; not saving"
        )
    return body


def generate_sidecar(
    video: str | Path,
    *,
    model: str = DEFAULT_MODEL,
    detail: str = "standard",
    api_key: str | None = None,
    provider: str = DEFAULT_PROVIDER,
    base_url: str | None = None,
    scene_threshold: float | None = None,
    effort: str | None = None,
    usage_out: dict | None = None,
) -> Sidecar:
    """Full pipeline: hash + probe + describe → a ready-to-save Sidecar.

    provider="gemini" uploads the video to the Gemini Files API (needs a key).
    provider="local" samples frames and posts them to an OpenAI-compatible
    endpoint (no key, no cost, footage stays on the machine).
    provider="claude" tiles frames into contact sheets and has Claude Code read
    them in print mode (no key; billed to the user's subscription).
    """
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {list(PROVIDERS)}")
    video = Path(video)
    header = {
        "video": video.name,
        "sha256": hash_file(video),
        "bytes": str(video.stat().st_size),
        **probe(video),
        "generator": model,
        "created": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "detail": detail,
        "lang": "en",
    }

    if provider == "claude":
        from .assemble import header_for
        from .claude import describe_video_claude

        body, manifest, source = describe_video_claude(
            video, detail=detail, model=model, effort=effort,
            scene_threshold=scene_threshold, usage_out=usage_out)
        generator = f"claude-code/{model}" if model else "claude-code"
        header = header_for(video, manifest, generator=generator, transcript_source=source)
        return Sidecar(version=SPEC_VERSION, header=header, body=body)

    if provider == "local":
        from . import local  # lazy: keeps ffmpeg/endpoint concerns out of the Gemini path

        threshold = (local.DEFAULT_SCENE_THRESHOLD if scene_threshold is None
                     else scene_threshold)
        body = local.describe_video_local(
            video, model=model, detail=detail,
            base_url=base_url or local.DEFAULT_BASE_URL,
            scene_threshold=threshold, usage_out=usage_out
        )
        header.update(local.local_header_extras(
            continuity=True, transcribed="(no speech)" not in body,
            threshold=threshold
        ))
    else:
        body = describe_video(
            video, model=model, detail=detail, api_key=api_key, usage_out=usage_out
        )

    return Sidecar(version=SPEC_VERSION, header=header, body=body)
