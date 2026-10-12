"""Unit tests for assembling an agent-written body into a sidecar. No ffmpeg, no model."""

import pytest

from cdaf import assemble as asm
from cdaf.prepare import Manifest, PrepareError, Shot, layout_sheets
from cdaf.sidecar import parse


def _manifest(speech=None) -> Manifest:
    shots = [Shot(n=1, start=0.0, end=4.0), Shot(n=2, start=4.0, end=8.0),
             Shot(n=3, start=8.0, end=12.0)]
    sheets = layout_sheets(shots, per_shot=2, cols=3, rows=2)
    return Manifest(video="clip.mp4", sha256="0" * 64, bytes=10, duration=12.0,
                    detail="standard", scene_threshold=0.15, max_shot=10.0, shots=shots,
                    sheets=sheets, speech=speech or [], audio=None, labeled=True)


GOOD_BODY = """## Summary
Three color cards.

## Segments
[00:00.0-00:04.0] Solid red frame.
[00:04.0-00:08.0] Solid blue frame with the title "OCEAN".
[00:08.0-00:12.0] Solid green frame.

## On-screen Text
[00:04.8] "OCEAN"

## Tags
red, blue, green
"""


# ---------------------------------------------------------------- parsing

def test_parse_timestamp_handles_minutes_and_hours():
    assert asm.parse_timestamp("00:04.2") == pytest.approx(4.2)
    assert asm.parse_timestamp("01:02:03.5") == pytest.approx(3723.5)


def test_split_sections_keys_by_heading():
    sections = asm.split_sections(GOOD_BODY)
    assert set(sections) == {"Summary", "Segments", "On-screen Text", "Tags"}
    assert sections["Summary"] == "Three color cards."


# ---------------------------------------------------------------- segments

def test_check_segments_accepts_matching_shots():
    assert asm.check_segments(_manifest(), asm.split_sections(GOOD_BODY)["Segments"]) == []


def test_check_segments_rejects_missing_shot():
    text = "[00:00.0-00:04.0] a\n[00:04.0-00:12.0] b"
    problems = asm.check_segments(_manifest(), text)
    assert problems and "expected 3" in problems[0]


def test_check_segments_rejects_shifted_boundary():
    text = "[00:00.0-00:04.0] a\n[00:04.0-00:08.5] b\n[00:08.5-00:12.0] c"
    problems = asm.check_segments(_manifest(), text)
    assert any("S2 must be [00:04.0-00:08.0]" in p for p in problems)


def test_check_segments_rejects_prose_lines():
    problems = asm.check_segments(_manifest(), "The first shot is red.")
    assert problems and "not a segment line" in problems[0]


# -------------------------------------------------------------- transcript

def test_transcript_without_speech_is_no_speech():
    lines, source = asm.transcript_section(_manifest(), None)
    assert lines == ["(no speech)"] and source == "none"


def test_transcript_with_speech_but_no_file_is_marked_untranscribed():
    lines, source = asm.transcript_section(_manifest(speech=[[1.0, 2.5]]), None)
    assert lines == ["[00:01.0-00:02.5] (untranscribed speech)"]
    assert source == "untranscribed"


def test_transcript_file_wins(tmp_path):
    (tmp_path / "transcript.txt").write_text("[00:01.0] Hello there.\n\n[0:02.5] Bye.\n",
                                             encoding="utf-8")
    lines = asm.read_transcript(tmp_path)
    assert lines == ["[00:01.0] Hello there.", "[00:02.5] Bye."]
    got, source = asm.transcript_section(_manifest(speech=[[1.0, 2.5]]), lines)
    assert got == lines and source == "transcript.txt"


def test_transcript_file_rejects_untimed_lines(tmp_path):
    (tmp_path / "transcript.txt").write_text("Hello there.\n", encoding="utf-8")
    with pytest.raises(asm.AssembleError):
        asm.read_transcript(tmp_path)


# ------------------------------------------------------------------- body

def test_body_from_text_builds_canonical_body():
    body, source = asm.body_from_text(_manifest(), GOOD_BODY, None)
    assert source == "none"
    assert body.split("\n")[0] == "## Summary"
    assert "## Transcript\n(no speech)" in body
    assert '## On-screen Text\n[00:04.8] "OCEAN"' in body
    assert body.endswith("## Tags\nred, blue, green")


def test_body_from_text_keeps_agent_transcript():
    text = GOOD_BODY + "\n## Transcript\n[00:01.0] Narrator: Hi.\n"
    body, source = asm.body_from_text(_manifest(speech=[[1.0, 2.0]]), text, None)
    assert source == "body"
    assert "[00:01.0] Narrator: Hi." in body


def test_body_from_text_requires_segments():
    with pytest.raises(asm.AssembleError):
        asm.body_from_text(_manifest(), "## Summary\nnothing\n", None)


def test_header_records_provenance(tmp_path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789")
    header = asm.header_for(video, _manifest(), generator="claude-code",
                            transcript_source="untranscribed")
    assert header["sha256"] == "0" * 64 and header["bytes"] == "10"
    assert header["x-shot-source"] == "ffmpeg-scene-detect@0.15"
    assert header["x-shot-isolation"] == "contact-sheet"
    assert header["x-transcript-timing"] == "measured-rms"


# --------------------------------------------------------------- assemble

def _work(tmp_path, manifest: Manifest, body: str = GOOD_BODY):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"0123456789")
    work = tmp_path / ".cdaf-work" / "clip.mp4"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")
    (work / "body.md").write_text(body, encoding="utf-8")
    return video, work


def test_assemble_writes_parsable_sidecar_and_cleans_up(tmp_path):
    from cdaf.sidecar import hash_file
    m = _manifest()
    video, work = _work(tmp_path, m)
    m.sha256 = hash_file(video)
    (work / "manifest.json").write_text(m.to_json(), encoding="utf-8")
    sc = asm.assemble(video)
    assert parse(asm.dumps(sc)).header["video"] == "clip.mp4"
    assert not work.exists()
    assert not (tmp_path / ".cdaf-work").exists()


def test_assemble_refuses_changed_video(tmp_path):
    video, work = _work(tmp_path, _manifest())      # manifest sha256 is all zeros
    with pytest.raises(PrepareError):
        asm.assemble(video)
    assert work.exists()


def test_assemble_reports_body_problems_and_keeps_work(tmp_path):
    from cdaf.sidecar import hash_file
    m = _manifest()
    video, work = _work(tmp_path, m, body="## Segments\n[00:00.0-00:12.0] everything\n")
    m.sha256 = hash_file(video)
    (work / "manifest.json").write_text(m.to_json(), encoding="utf-8")
    with pytest.raises(asm.AssembleError) as e:
        asm.assemble(video)
    assert "expected 3" in str(e.value)
    assert work.exists()
