from src.agent.knowledge_retriever import KnowledgeRetriever
from src.utils.telemetry_simulator import TelemetrySimulator


def test_wifi_degradation_retrieves_wifi_procedure():
    telemetry = TelemetrySimulator().get_telemetry("wifi_degradation")
    report = {
        "status": "DEGRADED",
        "fps": 12.0,
        "bitrate_kbps": 300.0,
        "sharpness_score": 100.0,
        "issues": ["Network packet loss"],
    }

    articles = KnowledgeRetriever().retrieve_guidance(report, telemetry)

    assert articles
    assert "KB-RING-001" in {article["id"] for article in articles}


import pytest

ABSENT = object()


def video_report(fps, bitrate):
    report = {"status": "DEGRADED", "sharpness_score": 100.0, "mean_brightness": 120.0, "issues": []}
    if fps is not ABSENT:
        report["fps"] = fps
    if bitrate is not ABSENT:
        report["bitrate_kbps"] = bitrate
    return report


def article_ids(report):
    return [article["id"] for article in KnowledgeRetriever().retrieve_guidance(report, None)]


@pytest.mark.parametrize("fps,bitrate", [
    (ABSENT, ABSENT), (None, None), (None, ABSENT), (ABSENT, None), (None, 1200.0), (30.0, None),
])
def test_missing_fps_or_bitrate_is_unknown_not_a_crash(fps, bitrate):
    # Unknown metrics add no evidence: same result as a report without them.
    assert article_ids(video_report(fps, bitrate)) == article_ids(video_report(ABSENT, ABSENT))


@pytest.mark.parametrize("fps,bitrate", [(10.0, None), (None, 100.0), (10.0, 100.0)])
def test_measured_low_fps_or_bitrate_still_scores_video_articles(fps, bitrate):
    assert "KB-RING-005" in article_ids(video_report(fps, bitrate))
    assert "KB-RING-005" not in article_ids(video_report(ABSENT, ABSENT))


def test_measured_healthy_fps_and_bitrate_add_no_evidence():
    assert article_ids(video_report(30.0, 1200.0)) == article_ids(video_report(ABSENT, ABSENT))
