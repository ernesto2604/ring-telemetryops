"""
G10 / section 9: the README states the same severity levels, labels and settings as the code.
The README severity table is rendered here from src/config/severity_thresholds.json and must
appear in the README row by row.
"""
import re
from pathlib import Path

from src.config import severity

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
ENV_EXAMPLE = (ROOT / ".env.example").read_text(encoding="utf-8")

INVERSE = {"lt": ">=", "lte": ">", "gt": "<=", "gte": "<"}


def _cond(op, value, unit):
    return f"`{severity.OP_SYMBOLS[op]} {value:g}` {unit}"


def readme_rows():
    """One markdown row per metric: GREEN | DEGRADED bands | CRITICAL band | domain | scope."""
    rows = []
    for name, rule in severity.METRICS.items():
        unit = rule["unit"]
        bands = rule["bands"]
        mildest = bands[-1]
        green = f"`{INVERSE[mildest['op']]} {mildest['value']:g}` {unit}"
        degraded, critical = [], "none"
        for i, band in enumerate(bands):
            text = f"{band['label']}: {_cond(band['op'], band['value'], unit)}"
            worse = bands[i - 1] if i else None
            if worse is not None:
                text += f" and `{INVERSE[worse['op']]} {worse['value']:g}`"
            if "when" in band:
                text += " (" + ", ".join(f"{k}={v}" for k, v in band["when"].items()) + " only)"
            if band["level"] == "CRITICAL":
                critical = text
            else:
                degraded.append(text)
        scope = []
        scope += [f"{k}={v}" for k, v in rule.get("when", {}).items()]
        scope += [f"not {k}={v}" for k, v in rule.get("unless", {}).items()]
        rows.append(f"| {rule['label']} (`{name}`) | {green} | {'; '.join(degraded) or 'none'} | {critical} | "
                    f"`{rule['domain']}` | {', '.join(scope) or 'always'} |")
    return rows


def test_readme_severity_table_matches_the_table():
    for row in readme_rows():
        assert row in README, f"README row missing or different:\n{row}"


def test_readme_states_the_heartbeat_rule():
    hb = severity.TABLE["heartbeat"]
    assert f"every {hb['period_seconds']} s" in README
    assert f"DEGRADED needs {hb['degraded_pulses']} consecutive heartbeats" in README
    assert "CRITICAL is diagnosed on the first heartbeat" in README


def test_readme_uses_the_domain_labels_of_the_table():
    for domain in ("NETWORK", "HARDWARE_POWER", "OPTICAL_PHYSICAL"):
        assert severity.DOMAIN_LABELS[domain] in README


def test_readme_has_no_stale_strings():
    for stale in ("≤5", "30fps", "day position", "Power Supply / Battery Failure",
                  "Optical / Physical / Lens Issue", "stuck in day", "Claude 3"):
        assert stale not in README, stale
    assert "Claude Haiku 4.5" in README


def test_env_example_and_readme_describe_the_same_variables():
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "BEDROCK_MODEL_ID",
                 "RING_DATA_SOURCE", "RING_PARTNER_ACCESS_TOKEN", "RING_PARTNER_DEVICE_ID"):
        assert name in ENV_EXAMPLE and name in README, name
    assert re.search(r"^# AWS_SESSION_TOKEN=$", ENV_EXAMPLE, re.M)
    assert "ASIA" in ENV_EXAMPLE and "AKIA" in ENV_EXAMPLE
    assert "AWS_SESSION_TOKEN" in README and "ASIA" in README


def test_readme_presents_the_fallback_as_a_mock_up():
    assert "Local fallback (mock-up)" in README
    assert "Bedrock (realistic mode)" in README
    assert "0.86" in README
