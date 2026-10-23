"""T39: "Was this helpful?" votes. The feedback file is always written under pytest's tmp_path."""
import json

import pytest
from fastapi.testclient import TestClient

import src.api.server as server
from src.api.funnel import funnel
from tests.test_api import NOMINAL_TELEMETRY, recording_concierge
from tests.test_video_pipeline import as_canvas_jpeg, dirty_lens, sample_frame

api = TestClient(server.app)


@pytest.fixture(autouse=True)
def feedback_file(tmp_path, monkeypatch):
    path = tmp_path / "feedback" / "feedback.jsonl"
    monkeypatch.setattr(server, "FEEDBACK_FILE", path)
    monkeypatch.setattr(server, "_feedback_times", server.deque())
    monkeypatch.setattr(server, "_recent_reports", server.OrderedDict())
    funnel.reset()
    yield path
    funnel.reset()


def report(monkeypatch, frame=None):
    concierge = recording_concierge(monkeypatch)
    monkeypatch.setattr(server, "SupportConcierge", lambda: concierge)
    body = api.post("/api/analyze", json={"source_mode": "twin", "camera_active": True,
                                          "frame_base64": as_canvas_jpeg(frame if frame is not None
                                                                         else dirty_lens(sample_frame())),
                                          "telemetry": NOMINAL_TELEMETRY}).json()
    return body["report_id"]


def vote(report_id, rating="up", **extra):
    return api.post("/api/feedback", json={"report_id": report_id, "rating": rating, **extra})


def test_every_report_has_a_server_uuid(monkeypatch):
    first, second = report(monkeypatch), report(monkeypatch, sample_frame())
    assert len(first) == 36 and first != second


def test_valid_vote_appends_one_line_without_image_device_or_ip(monkeypatch, feedback_file):
    report_id = report(monkeypatch)

    response = vote(report_id, "down", comment="  Lens was   clean already  ")

    assert response.status_code == 200 and response.json()["ok"] is True
    (line,) = feedback_file.read_text(encoding="utf-8").splitlines()
    saved = json.loads(line)
    assert set(saved) == {"time", "report_id", "engine", "severity", "domain", "rating", "comment"}
    assert saved["report_id"] == report_id and saved["engine"] == "bedrock" and saved["rating"] == "down"
    assert saved["severity"] in ("DEGRADED", "CRITICAL") and saved["domain"] == "OPTICAL_PHYSICAL"
    assert saved["comment"] == "Lens was clean already"
    assert "testclient" not in line and "a1b2c3d4e5f6" not in line
    m = api.get("/api/metrics").json()
    assert m["feedback_down"] == 1 and m["feedback_up"] == 0


def test_unknown_report_id_is_404(feedback_file):
    assert vote("00000000-0000-0000-0000-000000000000").status_code == 404
    assert not feedback_file.exists()


@pytest.mark.parametrize("rating", ["meh", "", None, 1])
def test_invalid_rating_is_422(monkeypatch, rating):
    assert vote(report(monkeypatch), rating).status_code == 422


def test_second_vote_on_the_same_report_is_409(monkeypatch, feedback_file):
    report_id = report(monkeypatch)
    assert vote(report_id).status_code == 200
    assert vote(report_id, "down").status_code == 409
    assert len(feedback_file.read_text(encoding="utf-8").splitlines()) == 1
    assert api.get("/api/metrics").json()["feedback_up"] == 1


def test_long_comment_is_cut_to_200_characters(monkeypatch, feedback_file):
    assert vote(report(monkeypatch), "down", comment="x" * 600).status_code == 200
    assert json.loads(feedback_file.read_text(encoding="utf-8"))["comment"] == "x" * 200


def test_oversized_comment_body_is_rejected(monkeypatch):
    assert vote(report(monkeypatch), "down", comment="x" * 5000).status_code == 422


def test_unwritable_file_is_a_clear_503_and_the_vote_can_be_retried(monkeypatch, tmp_path):
    report_id = report(monkeypatch)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file, not a directory")
    monkeypatch.setattr(server, "FEEDBACK_FILE", blocker / "feedback.jsonl")

    response = vote(report_id)

    assert response.status_code == 503 and "could not be saved" in response.json()["detail"]
    assert api.get("/api/metrics").json()["feedback_up"] == 0
    monkeypatch.setattr(server, "FEEDBACK_FILE", tmp_path / "ok" / "feedback.jsonl")
    assert vote(report_id).status_code == 200      # the server still works and the vote was not used up


def test_rate_limit_allows_30_votes_per_minute(monkeypatch):
    monkeypatch.setattr(server, "FEEDBACK_RATE_LIMIT", 3)
    ids = [report(monkeypatch, sample_frame()) for _ in range(4)]
    assert [vote(i).status_code for i in ids] == [200, 200, 200, 429]
    assert vote(ids[3]).headers.get("Retry-After") == "60"


def test_default_rate_limit_is_30_votes_per_minute():
    assert server.FEEDBACK_RATE_LIMIT == 30


def test_only_the_last_100_reports_accept_votes(monkeypatch):
    monkeypatch.setattr(server, "MAX_FEEDBACK_REPORTS", 2)
    first = report(monkeypatch, sample_frame())
    report(monkeypatch, sample_frame())
    report(monkeypatch, sample_frame())
    assert vote(first).status_code == 404


def test_feedback_directory_is_ignored_by_git():
    import subprocess

    out = subprocess.run(["git", "check-ignore", "data/feedback/feedback.jsonl"], cwd=server.ROOT,
                         capture_output=True, text=True)
    assert out.returncode == 0
