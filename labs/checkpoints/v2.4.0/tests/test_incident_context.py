import importlib.util
import json
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "incident_context.py"
_spec = importlib.util.spec_from_file_location("incident_context", SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
incident_context = importlib.util.module_from_spec(_spec)
sys.modules["incident_context"] = incident_context
_spec.loader.exec_module(incident_context)

from platformops.httpclient import HealthResult  # noqa: E402
from platformops.local_ops import GitStatusResult  # noqa: E402


def _fake_git_status(*, clean, changed_files=None, error=None):
    def fake(repo_path, *, timeout=10.0):
        return GitStatusResult(
            repo_path=repo_path,
            clean=clean,
            changed_files=changed_files or [],
            error=error,
        )

    return fake


def _fake_docker_info(*, available, containers_running=0, error=None):
    def fake():
        if not available:
            return {
                "available": False,
                "error": error or "docker daemon is not reachable",
            }
        return {"available": True, "ContainersRunning": containers_running}

    return fake


def _fake_check_health(*, ok, status_code=200, latency_ms=12.3, error=None):
    def fake(url, **kwargs):
        return HealthResult(
            url=url, ok=ok, status_code=status_code, latency_ms=latency_ms, error=error
        )

    return fake


def test_clean_repo_docker_up_and_healthy_endpoint_exits_0(monkeypatch):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context,
        "docker_info",
        _fake_docker_info(available=True, containers_running=3),
    )
    monkeypatch.setattr(incident_context, "check_health", _fake_check_health(ok=True))

    report, exit_code = incident_context.gather_report(
        repo_path=".", health_url="https://example.com/health"
    )

    assert exit_code == 0
    assert report["git"] == {"status": "CLEAN", "changed_files": []}
    assert report["docker"] == {"status": "AVAILABLE", "containers_running": 3}
    assert report["health"]["status"] == "OK"
    assert report["has_concern"] is False


def test_dirty_repo_marks_concern_and_exits_1(monkeypatch):
    monkeypatch.setattr(
        incident_context,
        "git_status",
        _fake_git_status(clean=False, changed_files=["M x.py"]),
    )
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )
    monkeypatch.setattr(incident_context, "check_health", _fake_check_health(ok=True))

    report, exit_code = incident_context.gather_report(repo_path=".", health_url=None)

    assert exit_code == 1
    assert report["git"]["status"] == "DIRTY"
    assert report["git"]["changed_files"] == ["M x.py"]
    assert report["has_concern"] is True


def test_docker_unavailable_marks_concern_and_exits_1(monkeypatch):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context,
        "docker_info",
        _fake_docker_info(available=False, error="docker daemon is not reachable"),
    )
    monkeypatch.setattr(incident_context, "check_health", _fake_check_health(ok=True))

    report, exit_code = incident_context.gather_report(repo_path=".", health_url=None)

    assert exit_code == 1
    assert report["docker"]["status"] == "UNAVAILABLE"
    assert report["docker"]["error"] == "docker daemon is not reachable"
    assert report["has_concern"] is True


def test_unhealthy_endpoint_marks_concern_and_exits_1(monkeypatch):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )
    monkeypatch.setattr(
        incident_context, "check_health", _fake_check_health(ok=False, status_code=500)
    )

    report, exit_code = incident_context.gather_report(
        repo_path=".", health_url="https://example.com/health"
    )

    assert exit_code == 1
    assert report["health"]["status"] == "UNHEALTHY"
    assert report["health"]["status_code"] == 500
    assert report["has_concern"] is True


def test_health_check_skipped_when_no_url_given(monkeypatch):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )

    report, exit_code = incident_context.gather_report(repo_path=".", health_url=None)

    assert exit_code == 0
    assert report["health"] == {
        "status": "SKIPPED",
        "url": None,
        "status_code": None,
        "latency_ms": None,
    }


def test_health_check_error_reports_unknown_and_is_not_a_concern(monkeypatch):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )
    monkeypatch.setattr(
        incident_context,
        "check_health",
        _fake_check_health(
            ok=False, status_code=None, latency_ms=None, error="timed out"
        ),
    )

    report, exit_code = incident_context.gather_report(
        repo_path=".", health_url="https://example.com/health"
    )

    assert exit_code == 0
    assert report["health"]["status"] == "UNKNOWN"
    assert report["health"]["error"] == "timed out"
    assert report["has_concern"] is False


def test_git_status_error_reports_unknown_and_is_not_a_concern(monkeypatch):
    monkeypatch.setattr(
        incident_context,
        "git_status",
        _fake_git_status(clean=False, error="not a git repository"),
    )
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )

    report, exit_code = incident_context.gather_report(
        repo_path="/tmp/not-a-repo", health_url=None
    )

    assert exit_code == 0
    assert report["git"]["status"] == "UNKNOWN"
    assert report["git"]["error"] == "not a git repository"
    assert report["has_concern"] is False


def test_main_prints_valid_json_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(incident_context, "git_status", _fake_git_status(clean=True))
    monkeypatch.setattr(
        incident_context, "docker_info", _fake_docker_info(available=True)
    )

    exit_code = incident_context.main(["--repo-path", "."])

    assert exit_code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["git"]["status"] == "CLEAN"
    assert printed["health"]["status"] == "SKIPPED"
