"""
T18: RING_DATA_SOURCE from .env reaches the server. Each case imports src.api.server in a fresh
subprocess (the pytest process has already imported it) from a copy of the repository with a fake
.env. Importing makes no network call: the Partner client only reads its token.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAKE_ENV = "RING_DATA_SOURCE=partner\nRING_PARTNER_ACCESS_TOKEN=fake-token\nRING_PARTNER_DEVICE_ID=fake-id\n"
CLEARED = ("RING_DATA_SOURCE", "RING_PARTNER_ACCESS_TOKEN", "RING_PARTNER_DEVICE_ID", "PYTHON_DOTENV_DISABLED",
           "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")


@pytest.fixture(scope="module")
def repo_copy(tmp_path_factory):
    """src, web and the knowledge base, with empty sample/upload folders and a fake .env."""
    root = tmp_path_factory.mktemp("repo")
    ignore = shutil.ignore_patterns("__pycache__")
    for name in ("src", "web"):
        shutil.copytree(ROOT / name, root / name, ignore=ignore)
    shutil.copytree(ROOT / "data" / "knowledge_base", root / "data" / "knowledge_base")
    for name in ("samples", "uploads", "frames"):
        (root / "data" / name).mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(FAKE_ENV, encoding="utf-8")
    return root


def server_source(root, **env):
    clean = {k: v for k, v in os.environ.items() if k not in CLEARED}
    clean.update(env)
    out = subprocess.run(
        [sys.executable, "-c", "import src.api.server as s; print('SOURCE=' + s.ring_source, s._ring_mode())"],
        cwd=root, env=clean, capture_output=True, text=True, timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout


def test_env_file_alone_selects_partner(repo_copy):
    stdout = server_source(repo_copy)
    assert "SOURCE=partner LIVE" in stdout
    assert "[Ring] Data source: partner (from .env)" in stdout


def test_shell_variable_still_wins_over_env_file(repo_copy):
    stdout = server_source(repo_copy, RING_DATA_SOURCE="simulator")
    assert "SOURCE=simulator MOCK" in stdout
    assert "from shell environment variable (overrides .env)" in stdout


def test_dotenv_disabled_still_skips_the_env_file(repo_copy):
    stdout = server_source(repo_copy, PYTHON_DOTENV_DISABLED="1")
    assert "SOURCE=simulator MOCK" in stdout
    assert ".env skipped because PYTHON_DOTENV_DISABLED is set" in stdout


def test_invalid_value_is_an_explicit_error(repo_copy):
    stdout = server_source(repo_copy, RING_DATA_SOURCE="parnter")
    assert "SOURCE=parnter ERROR" in stdout
