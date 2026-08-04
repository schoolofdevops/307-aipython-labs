"""platformops.diagnostics -- quick health check for a service definition file.

This answers one question fast: "is this service.yaml OK to deploy?" It
reuses `platformops.config`'s already-safe loader instead of opening the file
itself, so a missing file, a file this process cannot read, or broken YAML
never reaches this module as a raw exception -- it arrives as a
`ConfigError`, a type this module knows how to handle and report cleanly.

Logging here is for the operator running this in CI or on call, not for the
person reading the terminal -- `print()` is the report; `logger` calls are
extra detail, off by default, turned on with `--verbose`. Neither ever logs
a field's *value*, only the file path and which field had a problem, so a
service definition holding something sensitive never ends up in a log line.
"""

import argparse
import logging
import sys
from pathlib import Path

from platformops.config import ConfigError, load_yaml_dict
from platformops.servicedef import ServiceDefinition, validate_service

logger = logging.getLogger("platformops.diagnostics")


def diagnose(path: Path) -> int:
    """Run the health check on one service definition file.

    Returns the exit code this run should use: 0 if the file is a valid
    service definition, 1 for a file problem (missing, unreadable, invalid
    YAML) or a data problem (missing field, wrong type, a value the schema
    does not allow). Never raises for any of those -- only a genuine bug in
    this function itself reaches its caller as an exception.
    """
    logger.debug("loading %s", path)
    try:
        data = load_yaml_dict(path)
    except ConfigError as exc:
        logger.warning("config error for %s: %s", path, exc)
        print(f"{path}: ERROR -- {exc}")
        return 1

    result = validate_service(data)

    if isinstance(result, ServiceDefinition):
        logger.info("%s validated cleanly", path)
        print(f"{path}: OK -- {result.to_summary()}")
        return 0

    logger.warning("%s failed validation: %d field(s)", path, len(result))
    print(f"{path}: FAIL")
    for error in result:
        field = ".".join(str(part) for part in error["loc"])
        print(f"  {field}: {error['msg']}")
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platformops.diagnostics",
        description="Health check a service definition YAML file.",
    )
    parser.add_argument(
        "path", type=Path, help="path to a service definition YAML file"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show debug-level log detail (which file, which step) on stderr",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        force=True,  # re-apply the level on every call -- basicConfig() is a
        # no-op if the root logger already has a handler, which matters the
        # moment this runs more than once in the same process (a test suite,
        # a long-lived worker) -- without force=True, --verbose on a later
        # call would silently do nothing.
    )
    return diagnose(args.path)


if __name__ == "__main__":
    sys.exit(main())
