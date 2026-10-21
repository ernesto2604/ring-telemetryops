"""
severity.py — Ring TelemetryOps Concierge Agent
================================================
Single source of truth for the diagnostic severity levels (GREEN / DEGRADED / CRITICAL).

The numbers live in severity_thresholds.json next to this module. They are heuristics of this
project, not Ring specifications. The backend pre-check, the local fallback, the KB retriever and
the video analyzer read them from here; the web UI loads the same file through GET /api/thresholds.
"""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

TABLE: Dict[str, Any] = json.loads(
    Path(__file__).with_name("severity_thresholds.json").read_text(encoding="utf-8")
)

GREEN, DEGRADED, CRITICAL = "GREEN", "DEGRADED", "CRITICAL"
LEVEL_RANK = {GREEN: 0, DEGRADED: 1, CRITICAL: 2}
METRICS: Dict[str, Dict[str, Any]] = TABLE["metrics"]
DOMAIN_LABELS: Dict[str, str] = TABLE["domain_labels"]
PROVENANCE: str = TABLE["provenance"]
HEARTBEAT_PERIOD_SECONDS: int = TABLE["heartbeat"]["period_seconds"]
DEGRADED_PULSES: int = TABLE["heartbeat"]["degraded_pulses"]

_OPS = {
    "lt": lambda v, x: v < x,
    "lte": lambda v, x: v <= x,
    "gt": lambda v, x: v > x,
    "gte": lambda v, x: v >= x,
}
OP_SYMBOLS = {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}


def limit(metric: str, label: str) -> float:
    """The boundary value of the band `label` of `metric` (e.g. limit("rssi", "Poor") == -75)."""
    for band in METRICS[metric]["bands"]:
        if band["label"] == label:
            return band["value"]
    raise KeyError(f"{metric} has no band labelled {label!r}")


def _matches(condition: Optional[Dict[str, Any]], context: Dict[str, Any]) -> bool:
    return all(context.get(key) == value for key, value in (condition or {}).items())


def _applies(rule: Dict[str, Any], context: Dict[str, Any]) -> bool:
    """`when`: every key must equal the context; `unless`: not all keys may equal the context."""
    if "when" in rule and not _matches(rule["when"], context):
        return False
    if "unless" in rule and _matches(rule["unless"], context):
        return False
    return True


def classify_metric(metric: str, value: Any, context: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """
    Level of one measured value. Returns None when the value was not measured or the metric does
    not apply to this device (e.g. a transformer on a battery-only install): unknown is never GREEN.
    """
    rule = METRICS[metric]
    context = context or {}
    if value is None or isinstance(value, bool) or not _applies(rule, context):
        return None
    result = {"metric": metric, "value": value, "domain": rule["domain"], "unit": rule["unit"],
              "level": GREEN, "label": rule["green_label"], "reason": None, "op": None, "limit": None}
    for band in rule["bands"]:
        if _applies(band, context) and _OPS[band["op"]](float(value), float(band["value"])):
            result.update(level=band["level"], label=band["label"], reason=band["reason"],
                          op=band["op"], limit=band["value"])
            break
    return result


def classify_telemetry(metrics: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Every measured metric with its level, in table order. `metrics` uses the keys of
    concierge._telemetry_metrics (rssi, battery, transformer, ...) plus power_mode / chime_type.
    """
    context = {"power_mode": metrics.get("power_mode"), "chime_type": metrics.get("chime_type")}
    out = []
    for name, rule in METRICS.items():
        item = classify_metric(name, metrics.get(rule.get("value_key", name)), context)
        if item is not None:
            out.append(item)
    return out


def anomalies(classified: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [item for item in classified if item["level"] != GREEN]


def worst_level(levels: List[str]) -> str:
    return max(levels, key=LEVEL_RANK.__getitem__, default=GREEN)


def describe(item: Dict[str, Any]) -> str:
    """Technical one-liner for findings and prompts, e.g. 'Wi-Fi signal (RSSI) -84 dBm: Poor (DEGRADED, < -75 dBm)'."""
    rule = METRICS[item["metric"]]
    if item["level"] == GREEN:
        return f"{rule['label']} {item['value']:g} {rule['unit']}: {item['label']} (GREEN)"
    return (f"{rule['label']} {item['value']:g} {rule['unit']}: {item['label']} "
            f"({item['level']}, {OP_SYMBOLS[item['op']]} {item['limit']:g} {rule['unit']})")


def next_level_target(item: Dict[str, Any]) -> Optional[str]:
    """
    The boundary the metric must cross to reach the next better level, from the table only
    (e.g. RSSI -84 dBm, Poor -> 'at or above -75 dBm'). None when the metric is GREEN.
    """
    if item["level"] == GREEN:
        return None
    rule = METRICS[item["metric"]]
    # Bands run from worst to mildest, so leaving the current band means crossing its own boundary.
    better = {"lt": "at or above", "lte": "above", "gt": "at or below", "gte": "below"}[item["op"]]
    return f"{better} {item['limit']:g} {rule['unit']}"


def table_for_prompt() -> str:
    """The level table as plain text for the Bedrock prompt."""
    lines = []
    for name, rule in METRICS.items():
        bands = "; ".join(
            f"{band['level']} ({band['label']}) if {OP_SYMBOLS[band['op']]} {band['value']:g}" for band in rule["bands"]
        )
        scope = ""
        if "when" in rule:
            scope += " only when " + ", ".join(f"{k}={v}" for k, v in rule["when"].items())
        if "unless" in rule:
            scope += " not when " + ", ".join(f"{k}={v}" for k, v in rule["unless"].items())
        band_scope = [f"{band['label']} band only when " + ", ".join(f"{k}={v}" for k, v in band["when"].items())
                      for band in rule["bands"] if "when" in band]
        extra = f" ({'; '.join(band_scope)})" if band_scope else ""
        lines.append(f"- {rule['label']} [{rule['unit']}]{scope}: {bands}; otherwise GREEN{extra}.")
    return "\n".join(lines)
