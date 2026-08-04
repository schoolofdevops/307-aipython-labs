"""platformops.cli -- the `platformops` command.

Every capability this toolkit has built so far was a function you had to
import into a Python shell, or a module you ran with `python -m`. Real
operators do not want either -- they want one command, `platformops`, with
sub-commands underneath it, `--help` that explains itself, and exit codes a
CI job can trust. Typer builds that command from plain, type-hinted Python
functions: this module IS the CLI, and it is also still an importable,
testable module like every other one in this package.

Output has two audiences, and every command is built for both at once: a
human reads the plain-text report on a terminal; a script or a CI job reads
`--json` on stdout and the exit code, and nothing else. `--verbose` and
`--quiet` change how much a human sees; they never change what a machine
would parse, because a machine is only ever told to read `--json`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated

import typer

from platformops import __version__
from platformops.config import ConfigError, load_yaml_dict
from platformops.inventory.data import INVENTORY
from platformops.inventory.report import print_report
from platformops.inventory.rules import (
    count_by_environment,
    find_low_memory_prod,
    find_missing_owner,
)
from platformops.inventory.summary import build_summary
from platformops.servicedef import ServiceDefinition, validate_service

logger = logging.getLogger("platformops.cli")

app = typer.Typer(
    name="platformops",
    help="Inspect, validate and troubleshoot your services from one command line.",
    no_args_is_help=True,
)


def _fail(message: str, *, as_json: bool, exit_code: int = 1) -> None:
    """The one place every command's error path goes through.

    Five commands calling `typer.echo(...)` and `raise typer.Exit(...)`
    their own way would drift -- one forgets `err=True`, another spells the
    JSON envelope differently. Routing every command-level error (a file
    that cannot even be read, an option combination that makes no sense)
    through this one helper keeps the shape identical everywhere: plain
    text goes to stderr, so a shell pipeline never mistakes a failure
    message for real output; `--json` goes to stdout as one object with a
    `status` field a script can branch on; the exit code always matches.
    """
    if as_json:
        typer.echo(json.dumps({"status": "error", "error": message}))
    else:
        typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=exit_code)


@app.callback()
def main(
    ctx: typer.Context,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", help="show debug-level log detail on stderr"),
    ] = False,
    quiet: Annotated[
        bool,
        typer.Option("--quiet", help="only report a problem -- stay silent on success"),
    ] = False,
) -> None:
    """platformops -- inspect, validate and troubleshoot your services."""
    if verbose and quiet:
        _fail(
            "--verbose and --quiet cannot be used together", as_json=False, exit_code=2
        )

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        # Re-applies the level on every invocation -- basicConfig() is a
        # no-op once a handler exists, which matters the moment this runs
        # more than once in the same process (the test suite calls `app`
        # dozens of times). See platformops.diagnostics for the same fix.
        force=True,
    )
    ctx.obj = {"quiet": quiet}


@app.command()
def validate(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(help="path to a service definition YAML file")
    ],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
) -> None:
    """Validate a service definition file.

    Wraps the same loader and validator `platformops.diagnostics` uses
    (`load_yaml_dict` from M6, `validate_service` from M5) -- one validation
    path, whether you call it as a module or as this command.
    """
    quiet = ctx.obj["quiet"]

    try:
        data = load_yaml_dict(path)
    except ConfigError as exc:
        logger.warning("config error for %s: %s", path, exc)
        _fail(str(exc), as_json=as_json)

    result = validate_service(data)

    if isinstance(result, ServiceDefinition):
        logger.info("%s validated cleanly", path)
        if as_json:
            typer.echo(
                json.dumps(
                    {"status": "ok", "service": result.model_dump(mode="json")},
                    indent=2,
                )
            )
        elif not quiet:
            typer.echo(f"{path}: OK -- {result.to_summary()}")
        raise typer.Exit(code=0)

    # A FAIL is this command's normal report about the file's content --
    # not an exceptional condition -- so it stays on stdout, the same
    # convention `platformops.diagnostics` already uses. It is never
    # silenced by --quiet: quiet means "no chatter when everything is
    # fine", not "hide a real failure".
    logger.warning("%s failed validation: %d field(s)", path, len(result))
    if as_json:
        typer.echo(
            json.dumps({"status": "error", "errors": result}, indent=2, default=str)
        )
        raise typer.Exit(code=1)

    typer.echo(f"{path}: FAIL")
    for error in result:
        field = ".".join(str(part) for part in error["loc"])
        typer.echo(f"  {field}: {error['msg']}")
    raise typer.Exit(code=1)


def _inventory_payload() -> dict:
    """The same numbers `print_report()` prints, as a plain dict `--json` can dump."""
    return {
        "total_servers": len(INVENTORY),
        "by_environment": count_by_environment(INVENTORY),
        "missing_owner": find_missing_owner(INVENTORY),
        "low_memory_prod": find_low_memory_prod(INVENTORY),
        "summary": build_summary(INVENTORY),
    }


@app.command()
def inspect(
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable summary instead of the full report"
        ),
    ] = False,
) -> None:
    """Print the infrastructure inventory report (the M3/M4 project)."""
    if as_json:
        typer.echo(json.dumps(_inventory_payload(), indent=2))
        return
    print_report()


@app.command()
def version() -> None:
    """Print the installed platformops version."""
    typer.echo(f"platformops {__version__}")


if __name__ == "__main__":
    app()
