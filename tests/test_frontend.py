import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND_TESTS = Path(__file__).resolve().parent / "frontend" / "ui.test.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_frontend_ui_suite():
    """Runs the Node-based UI tests (fake DOM, mocked fetch, no network)."""
    result = subprocess.run(
        ["node", "--test", str(FRONTEND_TESTS)],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
