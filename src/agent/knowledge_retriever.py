"""
knowledge_retriever.py — Ring TelemetryOps Concierge Agent
===========================================================
Retrieval-Augmented Generation (RAG) Engine for Ring diagnostic standard operating procedures.
Cross-references multimodal metrics across 3 sources:
  1. diagnostic_report : Quantitative visual metrics from VideoAnalyzer (OpenCV)
  2. telemetry         : Ring Partner API device health + WebRTC W3C getStats()
  3. user_query / issue: Semantic and keyword matching across official Ring symptoms

Knowledge Base: data/knowledge_base/ring_video_troubleshooting.json (v2.1)
Official reference: Ring Help Center & Ring Support Articles.
"""

import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class KnowledgeRetriever:
    """
    RAG Retrieval Engine for official Ring troubleshooting SOPs.

    Official Ring thresholds:
      RSSI 0 to -60 dBm               -> Great
      RSSI -60 to -70 dBm             -> Okay
      RSSI -70 to -80 dBm             -> Poor (intermittent dropouts)
      RSSI < -80 dBm                  -> Very Poor (Ring App critical alert)
      Device Transmit Speed > 20 Mbps -> GOOD
      Device Transmit Speed 10-20 Mbps-> OKAY
      Device Transmit Speed < 10 Mbps -> POOR
      Internet Upload Speed > 10 Mbps -> GOOD
      Internet Upload Speed 5-10 Mbps -> OKAY
      Internet Upload Speed < 5 Mbps  -> POOR
    """

    # Official Ring RSSI thresholds
    RSSI_GREAT = -60   # dBm — Great (Ring doc)
    RSSI_OKAY  = -70   # dBm — Okay
    RSSI_POOR  = -80   # dBm — Poor / Very Poor boundary

    # Official Ring speed thresholds
    TX_GOOD    = 20    # Mbps device transmit — GOOD
    TX_OKAY    = 10    # Mbps device transmit — OKAY boundary
    UL_GOOD    = 10    # Mbps internet upload  — GOOD
    UL_OKAY    = 5     # Mbps internet upload  — OKAY boundary

    def __init__(self, kb_path: str = "data/knowledge_base/ring_video_troubleshooting.json"):
        self.kb_path = Path(kb_path)
        if not self.kb_path.exists():
            raise FileNotFoundError(f"Knowledge base not found at: {self.kb_path}")
        raw = self._load_raw()
        if isinstance(raw, dict):
            self.kb_meta    = raw
            self.kb_data    = raw.get("issues", [])
            self.kb_version = raw.get("knowledge_base_version", "2.1")
        else:
            self.kb_meta    = {}
            self.kb_data    = raw
            self.kb_version = "1.0"

    def _load_raw(self) -> Any:
        with open(self.kb_path, "r", encoding="utf-8") as f:
            return json.load(f)

    # -------------------------------------------------------------------------
    # Signal evaluation (official_signals)
    # -------------------------------------------------------------------------

    def _evaluate_signal(self, signal: Dict[str, Any], telemetry_flat: Dict[str, Any]) -> Optional[float]:
        """
        Evaluates an official_signal descriptor against flattened telemetry.
        Returns diagnostic_weight if operator condition holds, otherwise None.
        """
        metric   = signal.get("metric", "")
        operator = signal.get("operator", "")
        value    = signal.get("value")
        weight   = float(signal.get("diagnostic_weight", 0.0))

        current = telemetry_flat.get(metric)
        if current is None:
            return None

        try:
            if operator == "lte":
                return weight if float(current) <= float(value) else None
            elif operator == "gte":
                return weight if float(current) >= float(value) else None
            elif operator == "lt":
                return weight if float(current) < float(value) else None
            elif operator == "gt":
                return weight if float(current) > float(value) else None
            elif operator in ("equals", "eq", "=="):
                return weight if current == value else None
            elif operator in ("range", "between"):
                if isinstance(value, str) and "_to_" in value:
                    lo, hi = value.split("_to_")
                    lo_f = float(lo) if lo not in ("0", "") else float("-inf")
                    return weight if lo_f <= float(current) <= float(hi) else None
                elif isinstance(value, list) and len(value) == 2:
                    return weight if float(value[0]) <= float(current) <= float(value[1]) else None
        except (ValueError, TypeError):
            pass
        return None

    def _flatten_telemetry(self, telemetry: Dict[str, Any]) -> Dict[str, Any]:
        """
        Flattens multi-tiered Ring+WebRTC telemetry into single-level dictionary.
        """
        flat: Dict[str, Any] = {}
        dh  = telemetry.get("device_health", {})
        ws  = telemetry.get("webrtc_stats",  {})
        rtp = ws.get("inbound_rtp", {})
        cp  = ws.get("candidate_pair", {})

        flat.update({
            # Device Health
            "power_mode":                dh.get("power_mode", "wired"),
            "rssi_dbm":                  dh.get("rssi"),
            "battery_percentage":        dh.get("battery_percentage"),
            "battery_saver_low_voltage": dh.get("battery_saver_low_voltage"),
            "transformer_voltage_vac":   dh.get("transformer_voltage_vac"),
            "device_transmit_speed_mbps":dh.get("device_transmit_speed_mbps"),
            "internet_upload_speed_mbps":dh.get("internet_upload_speed_mbps"),
            "device_online":             dh.get("online"),
            "led_status":                dh.get("led_status"),
            "motion_detection_enabled":  dh.get("motion_detection_enabled"),
            "ring_plan_active":          dh.get("ring_plan_active"),
            "vpn_enabled":               dh.get("vpn_enabled"),
            # WebRTC
            "packet_loss_fraction":      rtp.get("packet_loss_fraction"),
            "jitter_ms":                 (rtp.get("jitter", 0.0) * 1000.0) if rtp.get("jitter") is not None else None,
            "rtt_ms":                    (cp.get("current_round_trip_time", 0.0) * 1000.0) if cp.get("current_round_trip_time") is not None else None,
            "bitrate_kbps":              rtp.get("bitrate_kbps"),
            "fps":                       rtp.get("frames_per_second"),
            "candidate_state":           cp.get("state"),
            # Hardware / Visual optical faults
            "optical_banding_detected":  bool(telemetry.get("optical_banding_detected", False)),
        })
        for k, v in telemetry.items():
            if k not in ("device_health", "webrtc_stats") and k not in flat:
                flat[k] = v
        return flat

    # -------------------------------------------------------------------------
    # Text / keyword search
    # -------------------------------------------------------------------------

    def search_by_query(self, query: str, top_k: int = 5) -> List[Dict[str, Any]]:
        """
        Text & keyword search over the Ring knowledge base.
        Evaluates titles, symptoms, retrieval keywords, and recommended actions.
        """
        if not query or not query.strip():
            return []

        tokens = [t.lower().strip() for t in query.split() if len(t.strip()) > 2]
        if not tokens:
            return []

        results: List[Tuple[float, Dict]] = []
        for article in self.kb_data:
            score = 0.0
            title    = article.get("title", "").lower()
            cat      = article.get("issue_category", "").lower()
            domain   = article.get("root_cause_domain", "").lower()
            symptoms = " ".join(article.get("symptoms", [])).lower()
            actions  = " ".join(article.get("recommended_actions", [])).lower()
            keywords = " ".join(article.get("retrieval_keywords", [])).lower()
            facts    = " ".join(article.get("official_facts", [])).lower()

            searchable = f"{title} {cat} {domain} {symptoms} {actions} {keywords} {facts}"

            for token in tokens:
                if token in title:
                    score += 4.0
                if token in keywords:
                    score += 3.0
                if token in symptoms:
                    score += 2.0
                if token in cat or token in domain:
                    score += 2.0
                if token in searchable:
                    score += 1.0

            if score > 0:
                results.append((score, article))

        results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in results[:top_k]]

    # -------------------------------------------------------------------------
    # Main retrieval
    # -------------------------------------------------------------------------

    def retrieve_guidance(
        self,
        diagnostic_report: Dict[str, Any],
        telemetry: Optional[Dict[str, Any]] = None,
        query: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves applicable Ring SOP articles correlating computer vision metrics,
        device health, WebRTC telemetry, and search queries.

        Args:
            diagnostic_report: Output of VideoAnalyzer.inspect_stream().
            telemetry:         Ring+WebRTC telemetry package (optional).
            query:             Text search query or customer issue description (optional).
            top_k:             Maximum number of SOP articles to return.

        Returns:
            Prioritized list of applicable Ring KB articles (up to top_k).
        """
        scored: Dict[str, Tuple[float, Dict]] = {}

        # 1. Text search
        if query:
            for rank, art in enumerate(self.search_by_query(query, top_k=10)):
                art_id = art.get("id", "")
                text_score = max(0.0, 1.0 - rank * 0.1)
                if art_id not in scored or scored[art_id][0] < text_score:
                    scored[art_id] = (text_score, art)

        if diagnostic_report.get("status") == "OPTIMAL" and telemetry is None and not query:
            return []

        # 2. Video metrics (OpenCV)
        fps             = diagnostic_report.get("fps", 30.0)
        bitrate         = diagnostic_report.get("bitrate_kbps", 500.0)
        sharpness       = diagnostic_report.get("sharpness_score", 100.0)
        mean_brightness = diagnostic_report.get("mean_brightness", 128.0)
        black_ratio     = diagnostic_report.get("black_pixel_ratio", 0.0)
        issues_text     = " ".join(diagnostic_report.get("issues", [])).lower()

        # 3. Telemetry metrics
        tel_flat: Dict[str, Any] = {}
        if telemetry:
            tel_flat = self._flatten_telemetry(telemetry)

        scenario = (telemetry or {}).get("scenario", "")

        # 4. Official signals evaluation per article
        for article in self.kb_data:
            art_id = article.get("id", "")
            signal_score = 0.0
            signal_hits  = 0

            for sig in article.get("official_signals", []):
                w = self._evaluate_signal(sig, tel_flat)
                if w is not None:
                    signal_score += w
                    signal_hits  += 1

            if signal_hits > 0:
                signal_score = signal_score / signal_hits
            else:
                signal_score = 0.0

            # 5. Domain heuristics (engineering thresholds)
            video_score = 0.0
            cat = article.get("issue_category", "")

            # Network-related
            if cat == "network":
                rssi = tel_flat.get("rssi_dbm", 0) or 0
                if rssi <= self.RSSI_POOR:
                    video_score += 0.9
                elif rssi <= self.RSSI_OKAY:
                    video_score += 0.5
                elif rssi <= self.RSSI_GREAT:
                    video_score += 0.2
                if scenario in ("wifi_degradation", "packet_loss_high"):
                    video_score += 0.5

            # Video quality & optics
            elif cat in ("video", "video_optics"):
                if diagnostic_report.get("status") != "STANDBY":
                    if sharpness < 20 or mean_brightness < 15 or black_ratio > 0.8:
                        video_score += 0.6
                    if bitrate < 150 or fps < 15:
                        video_score += 0.4
                    if any(kw in issues_text for kw in ("blur", "blurry", "dark", "black", "pixel", "banding", "flicker")):
                        video_score += 0.6
                    if diagnostic_report.get("optical_banding_detected") or tel_flat.get("optical_banding_detected"):
                        video_score += 0.95

            # Connectivity
            elif cat == "connectivity":
                dev_online = tel_flat.get("device_online")
                if dev_online is False:
                    video_score += 1.0
                elif scenario in ("live_view_timeout",):
                    video_score += 0.7

            # Power
            elif cat == "power":
                power_mode = tel_flat.get("power_mode", "wired")
                batt = tel_flat.get("battery_percentage", 100) or 100
                trans_v = tel_flat.get("transformer_voltage_vac")
                if power_mode == "wired" and trans_v is not None and trans_v < 14:
                    video_score += 1.0
                if batt < 10:
                    video_score += 0.9
                elif batt < 20:
                    video_score += 0.5
                if scenario in ("power_issue", "cold_battery_drain"):
                    video_score += 0.6

            # Motion & Notifications
            elif cat == "motion_and_notifications":
                if scenario in ("motion_delayed",):
                    video_score += 0.8
                if any(kw in issues_text for kw in ("motion", "pir", "detection")):
                    video_score += 0.4

            # Audio
            elif cat == "audio":
                if any(kw in issues_text for kw in ("audio", "echo", "microphone", "sound", "mic", "two-way")):
                    video_score += 0.7
                if scenario in ("audio_delay_jitter",):
                    video_score += 0.7

            combined = max(signal_score, video_score)

            for kw in article.get("retrieval_keywords", []):
                if kw.lower() in issues_text or kw.lower() in scenario:
                    combined = min(1.0, combined + 0.15)
                    break

            if combined > 0.1:
                if art_id not in scored or scored[art_id][0] < combined:
                    scored[art_id] = (combined, article)

        # 6. Sort, trim and return
        ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)
        return [item[1] for item in ranked[:top_k]]


if __name__ == "__main__":
    import os
    os.chdir(ROOT)

    from src.video_pipeline.analyzer import VideoAnalyzer
    from src.utils.telemetry_simulator import TelemetrySimulator

    video_path = sys.argv[1] if len(sys.argv) > 1 else "data/samples/bad_internet.mp4"
    scenario   = sys.argv[2] if len(sys.argv) > 2 else "wifi_degradation"

    analyzer  = VideoAnalyzer(video_path)
    report    = analyzer.inspect_stream()

    sim       = TelemetrySimulator()
    telemetry = sim.get_telemetry(scenario)

    retriever = KnowledgeRetriever()
    guidance  = retriever.retrieve_guidance(report, telemetry)

    print(f"KB Version        : {retriever.kb_version}")
    print(f"Total KB issues   : {len(retriever.kb_data)}")
    print(f"Video Stream      : {video_path}  |  Scenario: {scenario}")
    print(f"Analysis Status   : {report['status']}  |  Retrieved SOPs: {len(guidance)}")
    print()
    for g in guidance:
        print(f"  [{g.get('id','?')}] {g.get('title','?')} (Domain: {g.get('root_cause_domain','?')})")