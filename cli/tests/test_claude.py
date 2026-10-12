"""Unit tests for the Claude Code provider. No claude binary, no ffmpeg."""

import json
import subprocess

import pytest

from cdaf import claude as cc
from cdaf.generate import GenerationError
from cdaf.prepare import Manifest, Shot, layout_sheets


def _manifest(speech=None) -> Manifest:
    shots = [Shot(n=1, start=0.0, end=3.0), Shot(n=2, start=3.0, end=6.0)]
    sheets = layout_sheets(shots, per_shot=2, cols=3, rows=2)
    return Manifest(video="clip.mp4", sha256="0" * 64, bytes=10, duration=6.0,
                    detail="standard", scene_threshold=0.15, max_shot=10.0, shots=shots,
                    sheets=sheets, speech=speech or [], audio=None, labeled=True)


# ----------------------------------------------------------------- prompt

def test_prompt_names_every_sheet_and_shot(tmp_path):
    prompt = cc.build_prompt(tmp_path, _manifest())
    assert str(tmp_path / "sheet-01.jpg") in prompt
    assert "S1 [00:00.0-00:03.0]" in prompt and "S2 [00:03.0-00:06.0]" in prompt
    assert "Read tool" in prompt
    assert "English" in prompt


# ---------------------------------------------------------------- command

def test_command_is_read_only_and_non_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    cmd = cc.claude_command(cc.OUTPUT_SCHEMA, tmp_path, model="opus", effort="high")
    assert cmd[:2] == ["/usr/bin/claude", "-p"]
    assert "--no-session-persistence" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == cc.OUTPUT_SCHEMA
    assert cmd[cmd.index("--model") + 1] == "opus"
    assert cmd[cmd.index("--effort") + 1] == "high"
    assert "--bare" not in cmd


def test_command_omits_model_and_effort_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    cmd = cc.claude_command(cc.OUTPUT_SCHEMA, tmp_path, model=None, effort=None)
    assert "--model" not in cmd and "--effort" not in cmd


def test_command_requires_claude_on_path(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: None)
    with pytest.raises(GenerationError):
        cc.claude_command(cc.OUTPUT_SCHEMA, tmp_path, model=None, effort=None)


# -------------------------------------------------------------- run_claude

def _completed(stdout: str, returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_claude_prefers_structured_output(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    payload = {"is_error": False, "result": "ignored", "total_cost_usd": 0.1, "num_turns": 3,
               "duration_ms": 1500, "structured_output": {"summary": "s", "segments": [],
                                                           "screen_text": [], "tags": []}}
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: _completed(json.dumps(payload)))
    usage = {}
    out = cc.run_claude("prompt", tmp_path, model=None, effort=None, usage_out=usage)
    assert out["summary"] == "s"
    assert usage == {"claude_cost_usd": 0.1, "claude_turns": 3, "seconds": 1.5}


def test_run_claude_falls_back_to_json_in_result_text(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    payload = {"is_error": False, "result": 'Here you go: {"summary": "s", "segments": []}'}
    monkeypatch.setattr(cc.subprocess, "run", lambda *a, **k: _completed(json.dumps(payload)))
    assert cc.run_claude("p", tmp_path, model=None, effort=None)["summary"] == "s"


def test_run_claude_surfaces_login_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    payload = {"is_error": True, "result": "Not logged in · Please run /login"}
    monkeypatch.setattr(cc.subprocess, "run",
                        lambda *a, **k: _completed(json.dumps(payload), returncode=1))
    with pytest.raises(GenerationError) as e:
        cc.run_claude("p", tmp_path, model=None, effort=None)
    assert "Not logged in" in str(e.value)


def test_run_claude_reports_non_json_output(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.shutil, "which", lambda name: "/usr/bin/claude")
    monkeypatch.setattr(cc.subprocess, "run",
                        lambda *a, **k: _completed("", returncode=1, stderr="boom"))
    with pytest.raises(GenerationError) as e:
        cc.run_claude("p", tmp_path, model=None, effort=None)
    assert "boom" in str(e.value)


# ------------------------------------------------------------------- body

def test_body_from_output_orders_segments_by_manifest(tmp_path):
    out = {"summary": "Two cards.", "tags": ["a", "b"],
           "segments": [{"shot": 2, "description": "Blue."}, {"shot": 1, "description": "Red."}],
           "screen_text": [{"t": "00:03.6", "text": "OCEAN"}]}
    body, source = cc.body_from_output(_manifest(), out, tmp_path)
    assert "[00:00.0-00:03.0] Red.\n[00:03.0-00:06.0] Blue." in body
    assert '[00:03.6] "OCEAN"' in body
    assert body.endswith("## Tags\na, b")
    assert source == "none"


def test_body_from_output_rejects_missing_shot(tmp_path):
    out = {"summary": "", "tags": [], "screen_text": [],
           "segments": [{"shot": 1, "description": "Red."}]}
    with pytest.raises(GenerationError) as e:
        cc.body_from_output(_manifest(), out, tmp_path)
    assert "[2]" in str(e.value)


def test_body_from_output_records_untranscribed_speech(tmp_path):
    out = {"summary": "", "tags": [], "screen_text": [],
           "segments": [{"shot": 1, "description": "a"}, {"shot": 2, "description": "b"}]}
    body, source = cc.body_from_output(_manifest(speech=[[0.5, 2.0]]), out, tmp_path)
    assert "[00:00.5-00:02.0] (untranscribed speech)" in body
    assert source == "untranscribed"
