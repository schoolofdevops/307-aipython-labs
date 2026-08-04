import json
import subprocess

import pytest

from platformops.local_ops import (
    container_list,
    docker_info,
    git_log,
    git_status,
    scan_for_shell_true,
)


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def captured_subprocess_call(monkeypatch):
    """The stage crew: swaps subprocess.run for a spy that records exactly
    what it was called with, then hands back a clean, empty success result."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed(returncode=0, stdout="")

    monkeypatch.setattr("platformops.local_ops.subprocess.run", fake_run)
    return captured


def test_git_status_reports_clean_for_no_changes(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=""),
    )

    result = git_status("/some/repo")

    assert result.clean is True
    assert result.changed_files == []
    assert result.error is None


def test_git_status_reports_changed_files(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=" M src/foo.py\n?? new.txt\n"),
    )

    result = git_status("/some/repo")

    assert result.clean is False
    assert result.changed_files == [" M src/foo.py", "?? new.txt"]


def test_git_status_reports_error_for_non_git_directory(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=128, stderr="fatal: not a git repository"
        ),
    )

    result = git_status("/tmp")

    assert result.clean is False
    assert "not a git repository" in result.error


def test_git_status_reports_error_when_git_is_missing(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("platformops.local_ops.subprocess.run", raise_not_found)

    result = git_status("/some/repo")

    assert result.clean is False
    assert "not installed" in result.error


def test_git_status_reports_error_on_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=10.0)

    monkeypatch.setattr("platformops.local_ops.subprocess.run", raise_timeout)

    result = git_status("/some/repo")

    assert result.clean is False
    assert "timed out" in result.error


def test_git_status_never_uses_shell_true(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed(returncode=0, stdout="")

    monkeypatch.setattr("platformops.local_ops.subprocess.run", fake_run)

    git_status("/some/repo")

    assert isinstance(captured["args"], list)
    assert captured["kwargs"].get("shell", False) is False
    assert "timeout" in captured["kwargs"]


def test_git_log_returns_oneline_entries(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=0, stdout="abc1234 fix bug\ndef5678 add feature\n"
        ),
    )

    entries = git_log("/some/repo", n=2)

    assert entries == ["abc1234 fix bug", "def5678 add feature"]


def test_git_log_returns_empty_list_for_non_git_directory(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=128, stderr="fatal: not a git repository"
        ),
    )

    assert git_log("/tmp") == []


def test_docker_info_reports_available_and_merges_fields(monkeypatch):
    payload = json.dumps({"ServerVersion": "27.0.0", "Containers": 3})
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=payload),
    )

    info = docker_info()

    assert info["available"] is True
    assert info["ServerVersion"] == "27.0.0"


def test_docker_info_reports_unavailable_when_daemon_is_down(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=1, stderr="Cannot connect to the Docker daemon"
        ),
    )

    info = docker_info()

    assert info["available"] is False
    assert "Cannot connect" in info["error"]


def test_docker_info_reports_unavailable_when_docker_is_missing(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("platformops.local_ops.subprocess.run", raise_not_found)

    info = docker_info()

    assert info["available"] is False
    assert "not installed" in info["error"]


# --- container_list ---


def test_container_list_parses_ndjson_output(monkeypatch):
    ndjson = (
        '{"Names":"web","Status":"Up 2 hours"}\n{"Names":"db","Status":"Up 3 hours"}\n'
    )
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=ndjson),
    )

    result = container_list()

    assert len(result.containers) == 2
    assert result.containers[0]["Names"] == "web"
    assert result.error is None


def test_container_list_returns_empty_when_no_containers(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=""),
    )

    result = container_list()

    assert result.containers == []
    assert result.error is None


def test_container_list_skips_unparseable_lines(monkeypatch):
    ndjson = '{"Names":"web","Status":"Up"}\nNOT JSON\n{"Names":"db","Status":"Up"}\n'
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=0, stdout=ndjson),
    )

    result = container_list()

    assert len(result.containers) == 2
    assert result.error is None


def test_container_list_reports_error_when_docker_unreachable(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=1, stderr="Cannot connect to the Docker daemon"
        ),
    )

    result = container_list()

    assert result.containers == []
    assert "Cannot connect" in result.error


def test_container_list_reports_error_when_docker_missing(monkeypatch):
    def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr("platformops.local_ops.subprocess.run", raise_not_found)

    result = container_list()

    assert result.containers == []
    assert "not installed" in result.error


# --- scan_for_shell_true ---


def test_scan_for_shell_true_clean_when_no_match(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=1, stdout=""),
    )

    result = scan_for_shell_true("src")

    assert result.clean is True
    assert result.findings == []
    assert result.error is None


def test_scan_for_shell_true_finds_matches(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(
            returncode=0, stdout="src/bad.py:3:    subprocess.run(cmd, shell=True)\n"
        ),
    )

    result = scan_for_shell_true("src")

    assert result.clean is False
    assert len(result.findings) == 1
    assert "shell=True" in result.findings[0]


def test_scan_for_shell_true_reports_grep_error(monkeypatch):
    monkeypatch.setattr(
        "platformops.local_ops.subprocess.run",
        lambda *a, **k: _completed(returncode=2, stderr="grep: src: No such file"),
    )

    result = scan_for_shell_true("src")

    assert result.clean is False
    assert result.findings == []
    assert "No such file" in result.error


def test_docker_info_never_uses_shell_true_and_always_passes_a_timeout(
    captured_subprocess_call,
):
    docker_info()

    assert captured_subprocess_call["kwargs"].get("shell", False) is False
    assert "timeout" in captured_subprocess_call["kwargs"]


def test_container_list_never_uses_shell_true_and_always_passes_a_timeout(
    captured_subprocess_call,
):
    container_list()

    assert captured_subprocess_call["kwargs"].get("shell", False) is False
    assert "timeout" in captured_subprocess_call["kwargs"]
