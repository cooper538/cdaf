"""Unit tests for the prepare step. No ffmpeg, no model."""

import pytest

from cdaf import prepare as prep
from cdaf.prepare import Manifest, Shot, layout_sheets, plan_shots, split_long


# ------------------------------------------------------------------- shots

def test_split_long_keeps_short_spans():
    assert split_long([(0.0, 4.0), (4.0, 9.0)], max_len=10.0) == [(0.0, 4.0), (4.0, 9.0)]


def test_split_long_cuts_into_equal_parts_under_max():
    parts = split_long([(0.0, 25.0)], max_len=10.0)
    assert len(parts) == 3
    assert parts[0][0] == 0.0 and parts[-1][1] == 25.0
    for (_, end), (start, _) in zip(parts, parts[1:]):
        assert end == start
    assert all(b - a <= 10.0 + 0.01 for a, b in parts)


def test_plan_shots_without_cuts_still_samples_a_long_video():
    shots = plan_shots(60.0, [], "standard")
    assert len(shots) == 6
    assert shots[0].start == 0.0 and shots[-1].end == 60.0
    assert [s.n for s in shots] == list(range(1, 7))


def test_plan_shots_brief_merges_then_splits():
    shots = plan_shots(30.0, [1.0, 2.0, 3.0, 4.0], "brief")
    assert shots[0].start == 0.0 and shots[-1].end == 30.0
    assert all(s.end - s.start <= prep.DETAIL_MAX_SHOT["brief"] + 0.01 for s in shots)


# ------------------------------------------------------------------ sheets

def test_layout_sheets_fills_tiles_in_order_and_links_frames():
    shots = [Shot(n=i, start=float(i - 1) * 4, end=float(i) * 4) for i in range(1, 5)]
    sheets = layout_sheets(shots, per_shot=2, cols=3, rows=2)
    assert [s.file for s in sheets] == ["sheet-01.jpg", "sheet-02.jpg"]
    assert len(sheets[0].tiles) == 6 and len(sheets[1].tiles) == 2
    assert sheets[1].rows == 1
    assert [t["shot"] for t in sheets[0].tiles] == [1, 1, 2, 2, 3, 3]
    for shot in shots:
        assert len(shot.frames) == 2
        for frame in shot.frames:
            assert shot.start < frame.t < shot.end
    assert shots[3].frames[0].sheet == "sheet-02.jpg"
    assert shots[3].frames[0].tile == 1


def test_layout_sheets_single_frame_per_shot():
    shots = [Shot(n=1, start=0.0, end=2.0), Shot(n=2, start=2.0, end=3.0)]
    sheets = layout_sheets(shots, per_shot=1, cols=3, rows=2)
    assert len(sheets) == 1
    assert [t["shot"] for t in sheets[0].tiles] == [1, 2]


# ---------------------------------------------------------------- manifest

def _manifest() -> Manifest:
    shots = [Shot(n=1, start=0.0, end=4.0), Shot(n=2, start=4.0, end=8.0)]
    sheets = layout_sheets(shots, per_shot=1, cols=3, rows=2)
    return Manifest(video="clip.mp4", sha256="0" * 64, bytes=10, duration=8.0,
                    detail="standard", scene_threshold=0.15, max_shot=10.0, shots=shots,
                    sheets=sheets, speech=[[1.0, 2.5]], audio="audio.wav", labeled=True)


def test_manifest_round_trips_through_json():
    m = _manifest()
    back = Manifest.from_json(m.to_json())
    assert back.to_json() == m.to_json()
    assert back.shots[1].frames[0].sheet == "sheet-01.jpg"
    assert back.speech == [[1.0, 2.5]]


def test_manifest_rejects_unknown_version():
    text = _manifest().to_json().replace('"version": 1', '"version": 99')
    with pytest.raises(prep.PrepareError):
        Manifest.from_json(text)


def test_work_dir_is_hidden_next_to_the_video(tmp_path):
    video = tmp_path / "footage" / "clip.mp4"
    assert prep.work_dir_for(video) == tmp_path / "footage" / ".cdaf-work" / "clip.mp4"


def test_describe_work_lists_sheets_shots_and_speech(tmp_path):
    text = prep.describe_work(tmp_path, _manifest())
    assert "sheet-01.jpg" in text
    assert "S1 [00:00.0-00:04.0]" in text
    assert "S2 [00:04.0-00:08.0]" in text
    assert "00:01.0-00:02.5" in text
    assert "transcript.txt" in text


def test_drawtext_escapes_font_path_and_label():
    f = prep._drawtext("C:\\Windows\\Fonts\\consola.ttf", "S1 00:04.2")
    assert "fontfile='C\\:/Windows/Fonts/consola.ttf'" in f
    assert "text='S1 00\\:04.2'" in f
