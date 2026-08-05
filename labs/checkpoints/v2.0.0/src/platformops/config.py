"""Load, resolve and validate service definitions -- PlatformOps v0.4.

`servicedef.py` validates a service definition once it is already a Python
dict. Most of the time that dict starts life as a YAML file a team keeps in
its repository -- `service.yaml`, checked in next to the code it describes.
This module is the bridge between the two: it turns a path on disk into a
validated `ServiceDefinition`, and it is careful about the two ways a file
can fail before validation ever gets a chance to run -- the file might not
exist, or it might not be valid YAML.

It also resolves the config the way a real deploy pipeline does: values in
the YAML file can be overridden by environment variables (`PLATFORMOPS_*`),
never the other way round, and the resolved result can be written back out
to disk as one atomic operation -- a temp file written next to the target,
then renamed into place, so a crash mid-write never leaves a half-written
config for the next process to read.
"""

import argparse
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from platformops.servicedef import ServiceDefinition, validate_service

logger = logging.getLogger("platformops.config")

# Which ServiceDefinition fields an environment variable is allowed to
# override, and the PLATFORMOPS_<FIELD> name each one answers to. Kept to a
# fixed, explicit list on purpose -- an env var overriding a field nobody
# asked it to override is exactly the kind of surprise this module exists to
# prevent.
ENV_PREFIX = "PLATFORMOPS_"
ENV_OVERRIDE_FIELDS = ("environment", "region", "team_owner", "kubernetes_namespace")


class ConfigError(Exception):
    """A problem with the config file itself, not with the data in it.

    `validate_service` already reports data problems (a missing field, a
    bad environment value) as a list of error dicts. `ConfigError` is the
    base of a small family for everything upstream of that -- catch this one
    type and you catch every file-level problem this module can raise,
    without also swallowing a genuine bug somewhere else in the program.
    """


class ConfigNotFoundError(ConfigError):
    """The path does not exist."""


class ConfigPermissionError(ConfigError):
    """The path exists, but this process cannot read it."""


class ConfigParseError(ConfigError):
    """The file exists and is readable, but is not valid YAML."""


def load_yaml_dict(path: Path) -> dict[str, Any]:
    """Read a YAML file and return its top-level mapping as a dict.

    Raises one of `ConfigError`'s subclasses for every failure a learner (or
    a script) will actually hit before validation gets a chance to run: a
    missing file, a file this process cannot read, or text that is not valid
    YAML. Never logs the file's contents -- only its path -- so a config
    holding something sensitive never ends up in a log line by accident.
    """
    path = Path(path)
    logger.debug("loading %s", path)

    if not path.exists():
        raise ConfigNotFoundError(f"config file not found: {path}")

    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError as exc:
        raise ConfigPermissionError(
            f"cannot read {path}: permission denied "
            f"(check the file's permissions with `ls -l {path}`)"
        ) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigParseError(f"invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigParseError(
            f"{path} does not contain a YAML mapping (a service definition)"
        )

    return data


def load_service_yaml(path: Path) -> ServiceDefinition | list[dict[str, Any]]:
    """Load a service definition YAML file and validate it.

    Returns a validated `ServiceDefinition` on success, or the list of
    Pydantic error dicts on a data problem -- the same contract as
    `validate_service`. A file problem (missing file, broken YAML) raises
    `ConfigError` instead, so the two failure modes are never confused with
    each other.
    """
    data = load_yaml_dict(path)
    return validate_service(data)


def apply_env_overrides(
    data: dict[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Layer environment-variable overrides on top of file values.

    `environ` defaults to the real `os.environ`; a test passes its own dict
    instead so it never depends on -- or leaks into -- the actual process
    environment. Precedence is fixed: environment variable beats file value,
    a field the file never set is left alone, and a field this module was
    not told to watch (see `ENV_OVERRIDE_FIELDS`) can never be touched this
    way.
    """
    environ = os.environ if environ is None else environ
    resolved = dict(data)
    for field in ENV_OVERRIDE_FIELDS:
        env_name = f"{ENV_PREFIX}{field.upper()}"
        if env_name in environ:
            resolved[field] = environ[env_name]
    return resolved


def write_resolved_config(data: dict[str, Any], out_path: Path) -> None:
    """Write `data` to `out_path` as YAML -- atomically.

    A plain `out_path.write_text(...)` truncates the file the instant it
    opens for writing; a crash (power cut, OOM kill, `Ctrl-C`) partway
    through leaves a corrupt or empty file where a good one used to be.
    Writing to a temp file in the *same directory* and calling `os.replace`
    instead avoids that: `os.replace` is atomic on the same filesystem, so
    any process reading `out_path` sees either the old complete file or the
    new complete file, never a partial one. The Deep Dive proves this.
    """
    out_path = Path(out_path)
    fd, tmp_name = tempfile.mkstemp(
        dir=out_path.parent, prefix=f".{out_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
            yaml.safe_dump(data, tmp_file, sort_keys=False)
        os.replace(tmp_name, out_path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _report(
    path: Path, result: ServiceDefinition | list[dict[str, Any]], as_json: bool
) -> None:
    if isinstance(result, ServiceDefinition):
        if as_json:
            print(
                json.dumps(
                    {"status": "ok", "service": result.model_dump(mode="json")},
                    indent=2,
                )
            )
        else:
            print(f"{path}: OK -- {result.to_summary()}")
        return

    if as_json:
        print(json.dumps({"status": "error", "errors": result}, indent=2, default=str))
    else:
        print(f"{path}: FAIL")
        for error in result:
            location = ".".join(str(part) for part in error["loc"])
            print(f"  {location}: {error['msg']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platformops.config",
        description="Load, resolve and validate a service definition YAML file.",
    )
    parser.add_argument(
        "path", type=Path, help="path to a service definition YAML file"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="print machine-readable JSON instead of plain text",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate and resolve only -- report what would be written, write nothing",
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
        force=True,  # see platformops.diagnostics.main for why this matters
    )

    try:
        raw = load_yaml_dict(args.path)
    except ConfigError as exc:
        logger.warning("config error for %s: %s", args.path, exc)
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"{args.path}: ERROR -- {exc}")
        return 1

    resolved_data = apply_env_overrides(raw)
    result = validate_service(resolved_data)

    if isinstance(result, list):
        logger.warning("%s failed validation: %d field(s)", args.path, len(result))
        _report(args.path, result, args.json)
        return 1

    logger.info("%s validated cleanly", args.path)

    out_path = args.path.with_name(f"{args.path.stem}.resolved.yaml")

    if args.check:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "dry_run": True,
                        "would_write": str(out_path),
                        "service": result.model_dump(mode="json"),
                    },
                    indent=2,
                )
            )
        else:
            print(f"{args.path}: OK -- {result.to_summary()}")
            print(
                f"  (dry run -- would write resolved config to {out_path}, nothing written)"
            )
        return 0

    write_resolved_config(result.model_dump(mode="json"), out_path)
    if args.json:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "written": str(out_path),
                    "service": result.model_dump(mode="json"),
                },
                indent=2,
            )
        )
    else:
        print(f"{args.path}: OK -- {result.to_summary()}")
        print(f"  resolved config written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
