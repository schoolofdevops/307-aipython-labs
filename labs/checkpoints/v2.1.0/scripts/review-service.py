#!/usr/bin/env python3
"""Emit a fixed-format readiness report for one service definition file.

This script is a thin formatting adapter, not a second copy of the
validation logic. All rules about what makes a service definition valid
live in `platformops.servicedef` and `platformops.config` -- this script
only calls them, checks two things a schema pass can still miss (empty
observability fields), and turns the result into one JSON report with a
stable exit code. The `service-readiness-review` skill runs this script
and relays its report instead of parsing raw validator output itself.

Exit codes:
  0 -- the file is ready to ship
  1 -- the file was read, but validation or observability checks failed
  2 -- the file could not even be read (missing, unreadable, not valid YAML)
"""

import json
import sys
from pathlib import Path
from typing import Any

from platformops.config import ConfigError, load_yaml_dict
from platformops.servicedef import validate_service


def _report(
    file: str,
    validation: str,
    problems: list[str],
    dashboard_url_set: bool,
    alert_channel_set: bool,
    namespace: str | None,
    recommendation: str,
) -> dict[str, Any]:
    return {
        "file": file,
        "validation": validation,
        "problems": problems,
        "observability": {
            "dashboard_url_set": dashboard_url_set,
            "alert_channel_set": alert_channel_set,
        },
        "namespace": namespace,
        "recommendation": recommendation,
    }


def _observability_flags(data: dict[str, Any]) -> tuple[bool, bool]:
    """Check the raw observability fields directly, independent of schema validation.

    A file can fail schema validation on an unrelated field (a missing
    `deployment_name`, say) while still having real `dashboard_url` and
    `alert_channel` values -- the report should reflect what is actually in
    the file, not default both flags to false just because some other field
    was invalid.
    """
    observability = data.get("observability")
    if not isinstance(observability, dict):
        return False, False
    dashboard_url_set = bool(str(observability.get("dashboard_url") or "").strip())
    alert_channel_set = bool(str(observability.get("alert_channel") or "").strip())
    return dashboard_url_set, alert_channel_set


def review(path: Path) -> tuple[dict[str, Any], int]:
    """Load, validate and check one service definition file.

    Returns the JSON-ready report dict and the exit code that goes with it.
    """
    try:
        data = load_yaml_dict(path)
    except ConfigError as exc:
        report = _report(
            file=str(path),
            validation="FAIL",
            problems=[str(exc)],
            dashboard_url_set=False,
            alert_channel_set=False,
            namespace=None,
            recommendation="fix required before shipping",
        )
        return report, 2

    result = validate_service(data)

    if isinstance(result, list):
        dashboard_url_set, alert_channel_set = _observability_flags(data)
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in result
        ]
        report = _report(
            file=str(path),
            validation="FAIL",
            problems=problems,
            dashboard_url_set=dashboard_url_set,
            alert_channel_set=alert_channel_set,
            namespace=data.get("kubernetes_namespace"),
            recommendation="fix required before shipping",
        )
        return report, 1

    dashboard_url_set = bool(result.observability.dashboard_url.strip())
    alert_channel_set = bool(result.observability.alert_channel.strip())

    problems = []
    if not dashboard_url_set:
        problems.append("observability.dashboard_url: set but empty")
    if not alert_channel_set:
        problems.append("observability.alert_channel: set but empty")

    if problems:
        report = _report(
            file=str(path),
            validation="FAIL",
            problems=problems,
            dashboard_url_set=dashboard_url_set,
            alert_channel_set=alert_channel_set,
            namespace=result.kubernetes_namespace,
            recommendation="fix required before shipping",
        )
        return report, 1

    report = _report(
        file=str(path),
        validation="PASS",
        problems=[],
        dashboard_url_set=True,
        alert_channel_set=True,
        namespace=result.kubernetes_namespace,
        recommendation="ready to ship",
    )
    return report, 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: review-service.py <path-to-service.yaml>", file=sys.stderr)
        return 2

    report, exit_code = review(Path(argv[0]))
    print(json.dumps(report, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
