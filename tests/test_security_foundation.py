"""Security foundation tests: admin credential bootstrap and network binding.

These run independent Python interpreters with disposable data directories;
they never read, replace or log the host's actual admin token.
"""
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[1]
_READ = "from server.config import ADMIN_TOKEN, HOST; print(ADMIN_TOKEN); print(HOST)"


def _invoke(tmp_path, *, code=_READ, host=None):
    env = os.environ.copy()
    env["CVS_DATA_DIR"] = str(tmp_path / "private-data")
    env.pop("CVS_ADMIN_TOKEN", None)
    env.pop("CVS_HOST", None)
    if host is not None:
        env["CVS_HOST"] = host
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_REPO), env=env, text=True,
        capture_output=True, timeout=20,
    )


def test_simultaneous_cvs_processes_share_one_admin_credential(tmp_path):
    # The original read-if-missing -> write implementation could produce
    # multiple different ADMIN_TOKEN values during concurrent startup.
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: _invoke(tmp_path), range(8)))
    for result in results:
        assert result.returncode == 0, result.stderr
    tokens = [r.stdout.splitlines()[0] for r in results]
    assert len(set(tokens)) == 1
    assert len(tokens[0]) >= 32
    token_file = tmp_path / "private-data" / "admin-token.txt"
    assert token_file.read_text(encoding="utf-8").strip() == tokens[0]
    assert not list((tmp_path / "private-data").glob("*.tmp"))


def test_token_file_is_private_and_host_defaults_to_loopback(tmp_path):
    result = _invoke(tmp_path)
    assert result.returncode == 0, result.stderr
    token, host = result.stdout.splitlines()
    assert host == "127.0.0.1"
    assert token == (tmp_path / "private-data" / "admin-token.txt").read_text().strip()
    if os.name != "nt":
        token_mode = stat.S_IMODE((tmp_path / "private-data" / "admin-token.txt").stat().st_mode)
        directory_mode = stat.S_IMODE((tmp_path / "private-data").stat().st_mode)
        assert token_mode & 0o077 == 0
        assert directory_mode & 0o077 == 0


def test_explicit_network_binding_keeps_existing_clients_configurable(tmp_path):
    result = _invoke(tmp_path, host="0.0.0.0")
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[1] == "0.0.0.0"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file permission semantics")
def test_preexisting_insecure_token_is_preserved_and_restricted(tmp_path):
    folder = tmp_path / "private-data"
    folder.mkdir()
    token_file = folder / "admin-token.txt"
    token_file.write_text("preexisting-token-do-not-rotate\n", encoding="utf-8")
    token_file.chmod(0o644)
    result = _invoke(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "preexisting-token-do-not-rotate"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_interrupted_empty_credential_never_automatically_rotates(tmp_path):
    folder = tmp_path / "private-data"
    folder.mkdir()
    token_file = folder / "admin-token.txt"
    token_file.write_text("", encoding="utf-8")
    code = (
        "from unittest.mock import patch; "
        "from contextlib import contextmanager; "
        "with_patch = patch('time.sleep', lambda seconds: None); "
        "with_patch.start(); "
        "import server.config"
    )
    result = _invoke(tmp_path, code=code)
    assert result.returncode != 0
    assert "initialization incomplete" in result.stderr
    assert token_file.read_bytes() == b""


@pytest.mark.skipif(os.name == "nt", reason="test requires POSIX filesystem symlinks")
def test_admin_token_symlink_is_rejected_without_reading_target(tmp_path):
    folder = tmp_path / "private-data"
    folder.mkdir()
    target = tmp_path / "different-secret.txt"
    target.write_text("must-not-be-read\n", encoding="utf-8")
    (folder / "admin-token.txt").symlink_to(target)
    result = _invoke(tmp_path)
    assert result.returncode != 0
    assert "must not be a symlink" in result.stderr
    assert target.read_text() == "must-not-be-read\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX umask semantics")
def test_token_created_private_even_with_umask_zero(tmp_path):
    code = (
        "import os; os.umask(0); "
        "from server.config import ADMIN_TOKEN; print(ADMIN_TOKEN)"
    )
    result = _invoke(tmp_path, code=code)
    assert result.returncode == 0, result.stderr
    path = tmp_path / "private-data" / "admin-token.txt"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
