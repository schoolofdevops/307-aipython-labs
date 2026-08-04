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

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from platformops import __version__
from platformops.cloudinventory import scan_inventory
from platformops.config import ConfigError, load_yaml_dict
from platformops.httpclient import (
    DEFAULT_MAX_CONCURRENCY,
    EndpointStatusError,
    EndpointUnreachableError,
    HttpCheckError,
    check_health,
    check_many,
    check_many_async,
    check_many_sequential,
    get_repo_info,
    list_workflow_runs,
    summarize_health_results,
)
from platformops.inventory.data import INVENTORY
from platformops.inventory.report import print_report
from platformops.inventory.rules import (
    count_by_environment,
    find_low_memory_prod,
    find_missing_owner,
)
from platformops.inventory.summary import build_summary
from platformops.local_ops import (
    container_list,
    docker_info,
    git_status,
    scan_for_shell_true,
)
from platformops.reportstore import upload_report_to_bucket
from platformops.servicedef import ServiceDefinition, validate_service
from platformops.workqueue import receive_from_queue, send_to_queue

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


@app.command("http-check")
def http_check(
    ctx: typer.Context,
    url: Annotated[
        str, typer.Argument(help="URL to check, e.g. https://api.github.com")
    ],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout", help="seconds to wait for a response before giving up"
        ),
    ] = 10.0,
) -> None:
    """Check whether an HTTP endpoint is reachable and returns a healthy status.

    Exit 0 for a 2xx response, 1 for anything else -- an error status, a
    timeout, or a connection failure -- the same 0/1 contract `validate`
    uses for a service definition. Exit 2 for a URL this command will not
    even attempt, such as one missing a scheme.
    """
    quiet = ctx.obj["quiet"]

    if "://" not in url:
        _fail(
            f"'{url}' is missing a scheme -- did you mean 'https://{url}'?",
            as_json=as_json,
            exit_code=2,
        )

    result = check_health(url, timeout=timeout)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok" if result.ok else "error",
                    "url": result.url,
                    "status_code": result.status_code,
                    "latency_ms": result.latency_ms,
                    "error": result.error,
                },
                indent=2,
            )
        )
    elif result.ok:
        if not quiet:
            typer.echo(f"{url}: OK -- {result.status_code} in {result.latency_ms}ms")
    else:
        detail = result.error or f"status {result.status_code}"
        typer.echo(f"{url}: FAIL -- {detail}")

    raise typer.Exit(code=0 if result.ok else 1)


@app.command("health-check-many")
def health_check_many(
    ctx: typer.Context,
    urls: Annotated[
        list[str],
        typer.Argument(
            help="two or more URLs to check, e.g. https://a.example https://b.example"
        ),
    ],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
    mode: Annotated[
        str,
        typer.Option("--mode", help="sequential, threaded or async"),
    ] = "threaded",
    max_concurrency: Annotated[
        int,
        typer.Option(
            "--max-concurrency",
            help="how many checks run at once (threaded and async modes)",
        ),
    ] = DEFAULT_MAX_CONCURRENCY,
    timeout: Annotated[
        float,
        typer.Option("--timeout", help="seconds to wait for each endpoint's response"),
    ] = 10.0,
    batch_timeout: Annotated[
        float | None,
        typer.Option(
            "--batch-timeout",
            help="give up on the whole batch after this many seconds (async mode only)",
        ),
    ] = None,
) -> None:
    """Check the health of many endpoints at once, reporting partial failures instead of stopping at the first one.

    `--mode sequential` checks one URL at a time -- the baseline. `--mode
    threaded` (the default) runs a bounded thread pool; `--mode async` runs
    an `asyncio` event loop with the same concurrency cap. Exit 0 only if
    every endpoint answered healthy; exit 1 if one or more failed -- the
    command still prints every result either way, healthy and unhealthy
    endpoints alike, so a partial failure is never hidden behind the exit
    code alone.
    """
    quiet = ctx.obj["quiet"]

    bad_urls = [url for url in urls if "://" not in url]
    if bad_urls:
        _fail(
            f"'{bad_urls[0]}' is missing a scheme -- did you mean 'https://{bad_urls[0]}'?",
            as_json=as_json,
            exit_code=2,
        )

    if mode not in {"sequential", "threaded", "async"}:
        _fail(
            f"--mode must be sequential, threaded or async, got '{mode}'",
            as_json=as_json,
            exit_code=2,
        )

    if batch_timeout is not None and mode != "async":
        _fail(
            "--batch-timeout only applies to --mode async",
            as_json=as_json,
            exit_code=2,
        )

    if mode == "sequential":
        results = check_many_sequential(urls, timeout=timeout)
    elif mode == "threaded":
        results = check_many(urls, timeout=timeout, max_workers=max_concurrency)
    else:
        results = asyncio.run(
            check_many_async(
                urls,
                timeout=timeout,
                max_concurrency=max_concurrency,
                batch_timeout=batch_timeout,
            )
        )

    summary = summarize_health_results(results)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok" if summary.failed == 0 else "error",
                    "mode": mode,
                    "total": summary.total,
                    "ok": summary.ok,
                    "failed": summary.failed,
                    "results": [
                        {
                            "url": result.url,
                            "ok": result.ok,
                            "status_code": result.status_code,
                            "latency_ms": result.latency_ms,
                            "error": result.error,
                        }
                        for result in results
                    ],
                },
                indent=2,
            )
        )
    else:
        if not quiet or summary.failed:
            typer.echo(f"{summary.ok}/{summary.total} healthy ({mode})")
        for result in results:
            if result.ok:
                if not quiet:
                    typer.echo(
                        f"  {result.url}: OK -- {result.status_code} in {result.latency_ms}ms"
                    )
            else:
                detail = result.error or f"status {result.status_code}"
                typer.echo(f"  {result.url}: FAIL -- {detail}")

    raise typer.Exit(code=0 if summary.failed == 0 else 1)


@app.command("repo-info")
def repo_info(
    ctx: typer.Context,
    slug: Annotated[str, typer.Argument(help="owner/repo, e.g. httpx/httpx")],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
) -> None:
    """Fetch summary info for a public GitHub repository.

    No token is required for a public repository. Set GITHUB_TOKEN in the
    environment to raise the request's rate limit or reach a private repo
    you have access to -- the token is never printed or logged.
    """
    quiet = ctx.obj["quiet"]

    if "/" not in slug:
        _fail(f"expected owner/repo, got '{slug}'", as_json=as_json, exit_code=2)
    owner, _, repo = slug.partition("/")

    try:
        info = get_repo_info(owner, repo, token=os.environ.get("GITHUB_TOKEN"))
    except EndpointStatusError as exc:
        logger.warning("repo-info for %s failed: %s", slug, exc)
        _fail(str(exc), as_json=as_json)
    except (EndpointUnreachableError, HttpCheckError) as exc:
        logger.warning("repo-info for %s failed: %s", slug, exc)
        _fail(str(exc), as_json=as_json)

    if as_json:
        typer.echo(json.dumps({"status": "ok", "repo": info}, indent=2))
    elif not quiet:
        typer.echo(
            f"{info['full_name']}: {info['description'] or '(no description)'}\n"
            f"  default branch: {info['default_branch']}\n"
            f"  open issues: {info['open_issues_count']}  stars: {info['stargazers_count']}\n"
            f"  {info['html_url']}"
        )
    raise typer.Exit(code=0)


@app.command("workflow-runs")
def workflow_runs(
    ctx: typer.Context,
    slug: Annotated[
        str, typer.Argument(help="owner/repo, e.g. schoolofdevops/307-aipython-labs")
    ],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
    max_pages: Annotated[
        int,
        typer.Option("--max-pages", help="stop paging after this many pages"),
    ] = 5,
) -> None:
    """List recent GitHub Actions workflow runs for a public repository, following pagination."""
    quiet = ctx.obj["quiet"]

    if "/" not in slug:
        _fail(f"expected owner/repo, got '{slug}'", as_json=as_json, exit_code=2)
    owner, _, repo = slug.partition("/")

    try:
        runs = list_workflow_runs(
            owner, repo, token=os.environ.get("GITHUB_TOKEN"), max_pages=max_pages
        )
    except (EndpointStatusError, EndpointUnreachableError, HttpCheckError) as exc:
        logger.warning("workflow-runs for %s failed: %s", slug, exc)
        _fail(str(exc), as_json=as_json)

    if as_json:
        typer.echo(
            json.dumps({"status": "ok", "count": len(runs), "runs": runs}, indent=2)
        )
    elif not quiet:
        typer.echo(f"{slug}: {len(runs)} workflow run(s)")
        for run in runs:
            typer.echo(
                f"  #{run['id']} {run['name']} -- {run['status']}/{run['conclusion']} ({run['head_branch']})"
            )
    raise typer.Exit(code=0)


@app.command("local-status")
def local_status(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Option(
            "--path", help="directory to inspect (defaults to the current directory)"
        ),
    ] = Path("."),
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
) -> None:
    """Show local git and Docker state for a directory -- no network calls."""
    quiet = ctx.obj["quiet"]

    git_result = git_status(str(path))
    docker_result = docker_info()
    containers_result = container_list()

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "path": str(path),
                    "git": {
                        "clean": git_result.clean,
                        "changed_files": git_result.changed_files,
                        "error": git_result.error,
                    },
                    "docker": docker_result,
                    "containers": containers_result.containers,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0)

    if git_result.error:
        typer.echo(f"git: {git_result.error}")
    elif not quiet or not git_result.clean:
        state = (
            "clean"
            if git_result.clean
            else f"{len(git_result.changed_files)} changed file(s)"
        )
        typer.echo(f"git: {state}")
        for line in git_result.changed_files:
            typer.echo(f"  {line}")

    if docker_result.get("available"):
        if not quiet:
            version_str = docker_result.get("ServerVersion", "unknown version")
            typer.echo(f"docker: reachable ({version_str})")
    else:
        typer.echo(f"docker: {docker_result.get('error', 'not reachable')}")

    if containers_result.containers:
        for c in containers_result.containers:
            typer.echo(
                f"  container: {c.get('Names', 'unknown')} ({c.get('Status', 'unknown')})"
            )

    raise typer.Exit(code=0)


@app.command("check-security")
def check_security(
    ctx: typer.Context,
    path: Annotated[
        str,
        typer.Option("--path", help="source tree to scan (defaults to 'src')"),
    ] = "src",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print machine-readable JSON instead of plain text"
        ),
    ] = False,
) -> None:
    """Scan source files for unsafe subprocess usage (shell invocations)."""
    quiet = ctx.obj["quiet"]

    result = scan_for_shell_true(path)

    if result.error:
        _fail(result.error, as_json=as_json)

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok" if result.clean else "error",
                    "clean": result.clean,
                    "findings": result.findings,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0 if result.clean else 1)

    if result.clean:
        if not quiet:
            typer.echo("check-security: clean -- no unsafe shell usage found")
        raise typer.Exit(code=0)

    typer.echo(f"check-security: FAIL -- {len(result.findings)} finding(s)")
    for finding in result.findings:
        typer.echo(f"  {finding}")
    raise typer.Exit(code=1)


@app.command("inventory-scan")
def inventory_scan(
    ctx: typer.Context,
    region: Annotated[
        str, typer.Option("--region", help="AWS region to scan, e.g. us-east-1")
    ],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="named AWS profile to use (defaults to the normal credential chain)",
        ),
    ] = None,
    tag_key: Annotated[
        str | None,
        typer.Option("--tag-key", help="only include instances with this tag key"),
    ] = None,
    tag_value: Annotated[
        str | None,
        typer.Option(
            "--tag-value",
            help="only include instances with this tag key and value (requires --tag-key)",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Scan EC2 instances in one region into an inventory report (the M21 project).

    `--region` has no default -- every scan names the region it targets
    explicitly. No AWS key or secret is ever accepted as an option; the
    normal boto3 credential chain (`--profile`, environment variables, the
    shared config/credentials files, or an IAM role) resolves credentials
    for you.
    """
    quiet = ctx.obj["quiet"]

    if tag_value and not tag_key:
        _fail("--tag-value requires --tag-key", as_json=as_json, exit_code=2)

    result = scan_inventory(
        profile=profile, region=region, tag_key=tag_key, tag_value=tag_value
    )

    if result["status"] == "error":
        _fail(result["message"], as_json=as_json)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0)

    if not quiet:
        typer.echo(f"{result['count']} instance(s) in {result['region']}")
    for instance in result["instances"]:
        typer.echo(
            f"  {instance['instance_id']}: {instance['state']} tags={instance['tags']}"
        )
    raise typer.Exit(code=0)


@app.command("report-upload")
def report_upload(
    ctx: typer.Context,
    path: Annotated[Path, typer.Argument(help="path to a JSON report file")],
    bucket: Annotated[str, typer.Option("--bucket", help="S3 bucket to upload into")],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="named AWS profile to use (defaults to the normal credential chain)",
        ),
    ] = None,
    endpoint_url: Annotated[
        str | None,
        typer.Option(
            "--endpoint-url",
            help="override the AWS endpoint, e.g. http://localhost:4566 for Floci "
            "(defaults to AWS_ENDPOINT_URL, then real AWS)",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Upload a JSON report file to S3 under a timestamped key (the M22 project).

    The bucket is created automatically if it does not already exist. Point
    `--endpoint-url` at a local Floci instance (`http://localhost:4566`) to
    upload to a local, throwaway S3 bucket instead of a real one.
    """
    quiet = ctx.obj["quiet"]

    try:
        report = json.loads(path.read_text())
    except FileNotFoundError:
        _fail(f"{path}: no such file", as_json=as_json, exit_code=2)
    except json.JSONDecodeError as exc:
        _fail(f"{path}: not valid JSON -- {exc}", as_json=as_json, exit_code=2)

    result = upload_report_to_bucket(
        report, bucket=bucket, region=region, profile=profile, endpoint_url=endpoint_url
    )

    if result["status"] == "error":
        _fail(result["message"], as_json=as_json)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0)

    if not quiet:
        typer.echo(f"uploaded to s3://{result['bucket']}/{result['key']}")
    raise typer.Exit(code=0)


@app.command("queue-send")
def queue_send(
    ctx: typer.Context,
    queue_name: Annotated[str, typer.Argument(help="SQS queue name")],
    message: Annotated[str, typer.Argument(help="message body to send")],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="named AWS profile to use (defaults to the normal credential chain)",
        ),
    ] = None,
    endpoint_url: Annotated[
        str | None,
        typer.Option(
            "--endpoint-url",
            help="override the AWS endpoint, e.g. http://localhost:4566 for Floci "
            "(defaults to AWS_ENDPOINT_URL, then real AWS)",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Send one message to an SQS queue, creating the queue if it does not exist (the M22 project)."""
    quiet = ctx.obj["quiet"]

    result = send_to_queue(
        message,
        queue_name=queue_name,
        region=region,
        profile=profile,
        endpoint_url=endpoint_url,
    )

    if result["status"] == "error":
        _fail(result["message"], as_json=as_json)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0)

    if not quiet:
        typer.echo(f"sent to {result['queue']}: {result['message_id']}")
    raise typer.Exit(code=0)


@app.command("queue-receive")
def queue_receive(
    ctx: typer.Context,
    queue_name: Annotated[str, typer.Argument(help="SQS queue name")],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="named AWS profile to use (defaults to the normal credential chain)",
        ),
    ] = None,
    endpoint_url: Annotated[
        str | None,
        typer.Option(
            "--endpoint-url",
            help="override the AWS endpoint, e.g. http://localhost:4566 for Floci "
            "(defaults to AWS_ENDPOINT_URL, then real AWS)",
        ),
    ] = None,
    max_messages: Annotated[
        int,
        typer.Option("--max-messages", help="receive at most this many messages"),
    ] = 1,
    wait_time: Annotated[
        int | None,
        typer.Option(
            "--wait-time",
            help="long-poll for up to this many seconds if the queue is empty",
        ),
    ] = None,
    delete: Annotated[
        bool,
        typer.Option(
            "--delete",
            help="delete each message after receiving it (marks it processed)",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Receive messages from an SQS queue, creating the queue if it does not exist (the M22 project).

    Without `--delete`, a received message stays in the queue and its
    `ApproximateReceiveCount` grows on the next receive -- the same counter
    a queue's `RedrivePolicy` watches to decide when a message moves to its
    dead-letter queue.
    """
    quiet = ctx.obj["quiet"]

    result = receive_from_queue(
        queue_name=queue_name,
        region=region,
        profile=profile,
        endpoint_url=endpoint_url,
        max_messages=max_messages,
        wait_time_seconds=wait_time,
        delete=delete,
    )

    if result["status"] == "error":
        _fail(result["message"], as_json=as_json)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0)

    if not quiet:
        typer.echo(f"{len(result['messages'])} message(s) from {result['queue']}")
    for message in result["messages"]:
        typer.echo(f"  [{message['approximate_receive_count']}] {message['body']}")
    raise typer.Exit(code=0)


@app.command()
def version(
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Output version info as JSON"),
    ] = False,
) -> None:
    """Print the installed platformops version."""
    if json_output:
        import platform

        typer.echo(
            json.dumps({"version": __version__, "python": platform.python_version()})
        )
    else:
        typer.echo(f"platformops {__version__}")


if __name__ == "__main__":
    app()
