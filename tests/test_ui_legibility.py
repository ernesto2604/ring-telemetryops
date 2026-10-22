"""
T17: legibility of the Digital Twin UI (WCAG 2.2 AA). Reads the CSS colour tokens and checks the main
text/background pairs with the WCAG contrast formula, and that no font size is below 12 px.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STYLE = (ROOT / "web/css/style.css").read_text(encoding="utf-8")
SOURCES = [ROOT / "web/css/style.css", ROOT / "web/css/ring-device.css", ROOT / "web/css/video-monitor.css",
           ROOT / "web/index.html"]
MIN_FONT_PX = 12
ROOT_FONT_PX = 16
# Font sizes allowed below 12 px (icons or decorations without text). None today.
FONT_SIZE_EXCEPTIONS: set = set()

TOKENS = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", STYLE.split("}")[0]))


def luminance(hex_color):
    channels = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    r, g, b = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


@pytest.mark.parametrize("text", ["text-primary", "text-secondary", "text-muted"])
@pytest.mark.parametrize("background", ["bg-base", "bg-card", "bg-card-hover", "bg-input"])
def test_text_tokens_meet_aa_on_every_background(text, background):
    assert contrast(TOKENS[text], TOKENS[background]) >= 4.5, (text, background)


def test_dimmed_resolved_report_keeps_secondary_text_readable():
    rule = STYLE.split("#result-content.resolved .mitigation-box {")[1].split("}")[0]
    opacity = float(re.search(r"opacity:\s*([0-9.]+)", rule).group(1))
    card = TOKENS["bg-card"]
    for token in ("text-primary", "text-secondary", "ring-cyan"):
        r, g, b = (int(TOKENS[token][i:i + 2], 16) for i in (1, 3, 5))
        gray = 0.299 * r + 0.587 * g + 0.114 * b                        # filter: grayscale(1)
        mixed = "#" + "".join(f"{round(opacity * gray + (1 - opacity) * int(card[i:i + 2], 16)):02x}"
                              for i in (1, 3, 5))
        assert contrast(mixed, card) >= 3.0, token


def test_no_font_size_below_12px():
    small = []
    for path in SOURCES:
        for value, unit in re.findall(r"font-size:\s*([0-9.]+)(rem|px)", path.read_text(encoding="utf-8")):
            px = float(value) * (ROOT_FONT_PX if unit == "rem" else 1)
            if px < MIN_FONT_PX and f"{value}{unit}" not in FONT_SIZE_EXCEPTIONS:
                small.append(f"{path.name}: {value}{unit}")
    assert small == []
