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
from typing import Annotated, Any

import typer
from kubernetes.config.config_exception import ConfigException

from platformops import __version__, multiregion
from platformops.ai_inspect import inspect_ai_workload
from platformops.aiservice import validate_ai_service
from platformops.awsclient import get_aws_client
from platformops.cloudaudit import run_cloud_audit
from platformops.cloudinventory import scan_inventory
from platformops.cloudremediate import (
    DEFAULT_BATCH_CAP,
    BatchTooLargeError,
    FindingNotFoundError,
    RemediationNotApprovedError,
    RemediationPlan,
    RemediationResult,
    build_remediation_plan,
    execute_remediation_batch,
    execute_remediation_plan,
    find_finding,
    load_remediation_config,
    plan_to_dict,
    result_to_dict,
)
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
from platformops.incidentcontext import IncidentContextReport, collect_incident_context
from platformops.inventory.data import INVENTORY
from platformops.inventory.report import print_report
from platformops.inventory.rules import (
    count_by_environment,
    find_low_memory_prod,
    find_missing_owner,
)
from platformops.inventory.summary import build_summary
from platformops.k8sclient import get_kubernetes_clients
from platformops.kubernetes_inspect import inspect_workload
from platformops.kubernetes_restart import (
    RestartCoolingDownError,
    RestartNotApprovedError,
    RestartPlan,
    RestartResult,
    build_restart_plan,
    execute_restart,
    load_ops_config,
)
from platformops.kubernetes_restart import (
    plan_to_dict as restart_plan_to_dict,
)
from platformops.kubernetes_restart import (
    result_to_dict as restart_result_to_dict,
)
from platformops.local_ops import (
    container_list,
    docker_info,
    git_status,
    scan_for_shell_true,
)
from platformops.multiregion import ResourceRecord, scan_regions, to_csv, to_markdown
from platformops.observability import inspect_observability
from platformops.releasecheck import run_release_check
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


@app.command("multi-region-scan")
def multi_region_scan(
    ctx: typer.Context,
    regions: Annotated[
        str,
        typer.Option(
            "--regions", help="comma-separated AWS regions, e.g. us-east-1,eu-west-1"
        ),
    ],
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            help="named AWS profile to use (defaults to the normal credential chain)",
        ),
    ] = None,
    max_workers: Annotated[
        int,
        typer.Option(
            "--max-workers", help="maximum number of regions scanned concurrently"
        ),
    ] = multiregion.DEFAULT_MAX_WORKERS,
    output_format: Annotated[
        str,
        typer.Option("--format", help="markdown or csv (ignored with --json)"),
    ] = "markdown",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="print a machine-readable JSON report instead of markdown/csv",
        ),
    ] = False,
) -> None:
    """Scan EC2 instances, EBS volumes, security groups, Elastic IPs and S3 buckets across many regions (the M23 project).

    Regions are scanned concurrently, bounded by `--max-workers` -- never
    one thread per region. A region that fails (a `ClientError`, an
    expired session, missing credentials) is reported in the failed
    region list instead of aborting the scan; the command still exits 0
    only if every region succeeded.
    """
    quiet = ctx.obj["quiet"]

    region_list = [r.strip() for r in regions.split(",") if r.strip()]
    if not region_list:
        _fail("--regions must name at least one region", as_json=as_json, exit_code=2)

    if output_format not in {"markdown", "csv"}:
        _fail(
            f"--format must be markdown or csv, got '{output_format}'",
            as_json=as_json,
            exit_code=2,
        )

    result = scan_regions(region_list, profile=profile, max_workers=max_workers)

    if as_json:
        typer.echo(json.dumps(result, indent=2))
        raise typer.Exit(code=0 if not result["failed_regions"] else 1)

    records = [ResourceRecord(**entry) for entry in result["resources"]]
    typer.echo(to_markdown(records) if output_format == "markdown" else to_csv(records))

    if result["failed_regions"]:
        if not quiet:
            typer.echo("", err=True)
            typer.echo(f"{len(result['failed_regions'])} region(s) failed:", err=True)
        for failure in result["failed_regions"]:
            typer.echo(
                f"  {failure['region']}: {failure['error']} -- {failure['message']}",
                err=True,
            )
    raise typer.Exit(code=0 if not result["failed_regions"] else 1)


@app.command("cloud-audit")
def cloud_audit(
    ctx: typer.Context,
    policy: Annotated[
        Path, typer.Option("--policy", help="path to a policy YAML file")
    ],
    region: Annotated[
        str, typer.Option("--region", help="AWS region to audit, e.g. us-east-1")
    ],
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
    output: Annotated[
        Path | None,
        typer.Option("--output", help="also write the full JSON report to this path"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Check S3 buckets against a policy file and report violations (the M24 project).

    This command only reads and reports -- it never modifies, deletes or
    tags a resource. Exit 0 means no active (non-suppressed) findings;
    exit 1 means at least one. `--output` writes the same report
    `findings-list` and `findings-show` read back in, so a review can
    happen in a second, separate step.
    """
    quiet = ctx.obj["quiet"]

    report = run_cloud_audit(
        policy_path=policy, region=region, profile=profile, endpoint_url=endpoint_url
    )

    if report["status"] == "error":
        _fail(report["message"], as_json=as_json)

    if output is not None:
        output.write_text(json.dumps(report, indent=2))

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(code=0 if report["status"] == "ok" else 1)

    summary = report["summary"]
    if not quiet or summary["active"]:
        typer.echo(
            f"{summary['total_findings']} finding(s) -- {summary['active']} active, "
            f"{summary['suppressed']} suppressed"
        )
    for finding in report["findings"]:
        marker = "SUPPRESSED" if finding["suppressed"] else finding["severity"].upper()
        typer.echo(
            f"  [{marker}] {finding['resource_id']} ({finding['rule_id']}): "
            f"{finding['evidence']}"
        )
    raise typer.Exit(code=0 if report["status"] == "ok" else 1)


def _load_report(report_path: Path, *, as_json: bool) -> dict:
    try:
        text = report_path.read_text()
    except FileNotFoundError:
        _fail(f"{report_path}: no such file", as_json=as_json, exit_code=2)
    try:
        result: dict = json.loads(text)
    except json.JSONDecodeError as exc:
        _fail(f"{report_path}: not valid JSON -- {exc}", as_json=as_json, exit_code=2)
    return result


@app.command("findings-list")
def findings_list(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="path to a JSON report written by cloud-audit --output"),
    ],
    severity: Annotated[
        str | None,
        typer.Option("--severity", help="only show findings at this severity"),
    ] = None,
    include_suppressed: Annotated[
        bool,
        typer.Option(
            "--include-suppressed", help="also show findings an exception suppressed"
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """List findings from a report `cloud-audit --output` wrote -- active findings only, unless told otherwise."""
    quiet = ctx.obj["quiet"]
    report = _load_report(report_path, as_json=as_json)

    findings = report["findings"]
    if not include_suppressed:
        findings = [f for f in findings if not f["suppressed"]]
    if severity is not None:
        findings = [f for f in findings if f["severity"] == severity]

    if as_json:
        typer.echo(
            json.dumps(
                {"status": "ok", "count": len(findings), "findings": findings}, indent=2
            )
        )
        raise typer.Exit(code=0)

    if not quiet:
        typer.echo(f"{len(findings)} finding(s)")
    for finding in findings:
        marker = "SUPPRESSED" if finding["suppressed"] else finding["severity"].upper()
        typer.echo(
            f"  [{marker}] {finding['resource_id']} ({finding['rule_id']}): "
            f"{finding['evidence']}"
        )
    raise typer.Exit(code=0)


@app.command("findings-show")
def findings_show(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="path to a JSON report written by cloud-audit --output"),
    ],
    resource_id: Annotated[str, typer.Argument(help="the finding's resource_id")],
    rule_id: Annotated[str, typer.Argument(help="the finding's rule_id")],
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Show one finding's full evidence -- the (resource_id, rule_id) pair identifies it uniquely."""
    report = _load_report(report_path, as_json=as_json)

    matching = [
        f
        for f in report["findings"]
        if f["resource_id"] == resource_id and f["rule_id"] == rule_id
    ]
    if not matching:
        _fail(
            f"no finding for resource '{resource_id}' rule '{rule_id}' in {report_path}",
            as_json=as_json,
        )

    finding = matching[0]
    if as_json:
        typer.echo(json.dumps({"status": "ok", "finding": finding}, indent=2))
        raise typer.Exit(code=0)

    typer.echo(
        f"{finding['resource_id']} -- {finding['rule_id']} ({finding['severity']})"
    )
    typer.echo(f"  evidence: {finding['evidence']}")
    if finding["suppressed"]:
        typer.echo(f"  suppressed: {finding['suppression_reason']}")
    raise typer.Exit(code=0)


def _print_plan(plan: RemediationPlan, *, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps({"status": "ok", "plan": plan_to_dict(plan)}, indent=2))
        return
    typer.echo(f"{plan.finding_id} ({plan.rule_id}) -- {plan.status}")
    if plan.status == "remediable" and plan.action is not None:
        typer.echo(f"  would call: {plan.action.api_call}")
        typer.echo(f"  args: {json.dumps(plan.action.args)}")
    if plan.reason:
        typer.echo(f"  reason: {plan.reason}")
    if plan.before_state is not None:
        typer.echo(f"  before: {json.dumps(plan.before_state)}")


def _print_result(result: RemediationResult, *, as_json: bool) -> None:
    if as_json:
        typer.echo(
            json.dumps({"status": "ok", "result": result_to_dict(result)}, indent=2)
        )
        return
    typer.echo(f"{result.finding_id} ({result.rule_id}) -- {result.status}")
    if result.action:
        typer.echo(f"  action: {result.action}")
    if result.before_state is not None:
        typer.echo(f"  before: {json.dumps(result.before_state)}")
    if result.after_state is not None:
        typer.echo(f"  after:  {json.dumps(result.after_state)}")
    if result.verified is not None:
        typer.echo(f"  verified: {result.verified}")
    if result.message:
        typer.echo(f"  {result.message}")


def _print_refused(exc: RemediationNotApprovedError, *, as_json: bool) -> None:
    """Print the plan an un-approved execute call was about to refuse -- same shape either mode."""
    if as_json:
        typer.echo(
            json.dumps(
                {"status": "error", "error": str(exc), "plan": plan_to_dict(exc.plan)},
                indent=2,
            )
        )
    else:
        _print_plan(exc.plan, as_json=False)
        typer.echo(f"Error: {exc}", err=True)


@app.command("remediate-plan")
def remediate_plan(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="path to a JSON report written by cloud-audit --output"),
    ],
    finding_id: Annotated[
        str,
        typer.Argument(
            help="a finding's id, '<resource_id>:<rule_id>' -- see findings-list"
        ),
    ],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    remediation_config: Annotated[
        Path,
        typer.Option(
            "--remediation-config",
            help="path to a remediation allowlist/policy YAML file",
        ),
    ] = Path("remediation.example.yaml"),
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
    """Show what remediating one finding would do -- always a dry run, safe to run any number of times.

    This command never mutates anything. It reads the finding's current
    live state and reports `remediable` (with the exact API call it would
    make), `already_fixed` (nothing left to do), or `not_supported` (this
    rule is never auto-remediated -- see the reason). Nothing here is a
    substitute for `remediate-execute --approve`.
    """
    report = _load_report(report_path, as_json=as_json)
    try:
        finding = find_finding(report, finding_id)
    except FindingNotFoundError as exc:
        _fail(str(exc), as_json=as_json)

    config = load_remediation_config(remediation_config)
    client = get_aws_client(
        "s3", region=region, profile=profile, endpoint_url=endpoint_url
    )
    plan = build_remediation_plan(
        client, finding, allowlist=config["allowlist"], remediation_config=config
    )

    _print_plan(plan, as_json=as_json)
    raise typer.Exit(code=0)


@app.command("remediate-execute")
def remediate_execute(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="path to a JSON report written by cloud-audit --output"),
    ],
    finding_id: Annotated[
        str,
        typer.Argument(
            help="a finding's id, '<resource_id>:<rule_id>' -- see findings-list"
        ),
    ],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            help="required to actually mutate anything -- omit it to see the plan and exit non-zero",
        ),
    ] = False,
    remediation_config: Annotated[
        Path,
        typer.Option(
            "--remediation-config",
            help="path to a remediation allowlist/policy YAML file",
        ),
    ] = Path("remediation.example.yaml"),
    audit_log: Annotated[
        Path,
        typer.Option(
            "--audit-log",
            help="JSON-lines file every executed remediation is appended to",
        ),
    ] = Path("remediation-audit.jsonl"),
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
    """Execute the remediation for one finding -- refuses to mutate anything without --approve.

    Without `--approve`, this command prints the exact plan it would have
    run and exits non-zero -- it never silently does nothing and reports
    success. With `--approve`, it re-checks the finding against current
    live state right before mutating (running this twice on an
    already-fixed finding is safe -- the second run reports
    `already_fixed`), performs the change, and appends one line to
    `--audit-log` with the before/after evidence. That log line is the
    rollback record.
    """
    report = _load_report(report_path, as_json=as_json)
    try:
        finding = find_finding(report, finding_id)
    except FindingNotFoundError as exc:
        _fail(str(exc), as_json=as_json)

    config = load_remediation_config(remediation_config)
    client = get_aws_client(
        "s3", region=region, profile=profile, endpoint_url=endpoint_url
    )
    plan = build_remediation_plan(
        client, finding, allowlist=config["allowlist"], remediation_config=config
    )

    try:
        result = execute_remediation_plan(
            client,
            plan,
            approve=approve,
            allowlist=config["allowlist"],
            remediation_config=config,
            audit_log_path=audit_log,
            actor=os.environ.get("USER", "cli-operator"),
        )
    except RemediationNotApprovedError as exc:
        _print_refused(exc, as_json=as_json)
        raise typer.Exit(code=1) from None

    _print_result(result, as_json=as_json)
    raise typer.Exit(code=0 if result.status in {"executed", "already_fixed"} else 1)


@app.command("remediate-execute-batch")
def remediate_execute_batch(
    ctx: typer.Context,
    report_path: Annotated[
        Path,
        typer.Argument(help="path to a JSON report written by cloud-audit --output"),
    ],
    finding_ids: Annotated[
        list[str],
        typer.Argument(
            help="two or more finding ids, e.g. bucket-a:required-tags bucket-b:require-encryption"
        ),
    ],
    region: Annotated[str, typer.Option("--region", help="AWS region, e.g. us-east-1")],
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            help="required to actually mutate anything -- omit it to see the plans and exit non-zero",
        ),
    ] = False,
    remediation_config: Annotated[
        Path,
        typer.Option(
            "--remediation-config",
            help="path to a remediation allowlist/policy YAML file",
        ),
    ] = Path("remediation.example.yaml"),
    audit_log: Annotated[
        Path,
        typer.Option(
            "--audit-log",
            help="JSON-lines file every executed remediation is appended to",
        ),
    ] = Path("remediation-audit.jsonl"),
    max_batch: Annotated[
        int,
        typer.Option(
            "--max-batch",
            help="refuse a batch larger than this instead of truncating it -- "
            "raise it explicitly to remediate more findings in one run",
        ),
    ] = DEFAULT_BATCH_CAP,
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
    """Plan and execute remediation for several findings in one run -- refuses outright over --max-batch.

    A batch larger than `--max-batch` (default 5, the project's hard
    default cap) is refused before touching anything -- it is never
    silently truncated down to the cap and run anyway. Pass a higher
    `--max-batch` explicitly to remediate more findings in one invocation.
    Every finding in the batch still goes through the same allowlist gate
    and the same re-check-before-mutating idempotency as a single
    `remediate-execute` call.
    """
    report = _load_report(report_path, as_json=as_json)
    config = load_remediation_config(remediation_config)
    client = get_aws_client(
        "s3", region=region, profile=profile, endpoint_url=endpoint_url
    )

    plans = []
    for finding_id in finding_ids:
        try:
            finding = find_finding(report, finding_id)
        except FindingNotFoundError as exc:
            _fail(str(exc), as_json=as_json)
        plans.append(
            build_remediation_plan(
                client,
                finding,
                allowlist=config["allowlist"],
                remediation_config=config,
            )
        )

    try:
        results = execute_remediation_batch(
            client,
            plans,
            approve=approve,
            allowlist=config["allowlist"],
            remediation_config=config,
            audit_log_path=audit_log,
            actor=os.environ.get("USER", "cli-operator"),
            cap=max_batch,
        )
    except BatchTooLargeError as exc:
        _fail(str(exc), as_json=as_json)
    except RemediationNotApprovedError as exc:
        _print_refused(exc, as_json=as_json)
        raise typer.Exit(code=1) from None

    if as_json:
        typer.echo(
            json.dumps(
                {"status": "ok", "results": [result_to_dict(r) for r in results]},
                indent=2,
            )
        )
    else:
        for result in results:
            _print_result(result, as_json=False)
    raise typer.Exit(
        code=0 if all(r.status in {"executed", "already_fixed"} for r in results) else 1
    )


@app.command("release-check")
def release_check(
    ctx: typer.Context,
    service: Annotated[
        str,
        typer.Argument(
            help="service name, e.g. payments -- also the default repo name"
        ),
    ],
    owner: Annotated[
        str,
        typer.Option(
            "--owner", help="GitHub organization or user that owns the repository"
        ),
    ],
    pr: Annotated[
        int,
        typer.Option(
            "--pr", help="pull request number this release is tracked against"
        ),
    ],
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="repository name, if different from the service name"
        ),
    ] = None,
    branch: Annotated[
        str, typer.Option("--branch", help="branch this release is cut from")
    ] = "main",
    environment: Annotated[
        str,
        typer.Option(
            "--environment", help="deployment environment to check, e.g. production"
        ),
    ] = "production",
    artifact_name: Annotated[
        str | None,
        typer.Option(
            "--artifact-name",
            help="expected build artifact name (defaults to '<service>-build')",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Combine PR, CI, check-run, artifact, release and deployment evidence into one release verdict.

    Read-only: this command never merges the pull request, never re-runs a
    check, never triggers a build and never creates a deployment -- it only
    reads what GitHub's own CI/CD state already says. A source this command
    cannot reach (a timeout, a rate limit, an auth failure) is reported as
    `UNKNOWN` for that one section, never silently treated as a pass -- see
    `platformops.releasecheck` for the full fetch/evaluate split. Exit 0
    only when every gating section (`pr`, `ci`, `checks`, `artifacts`,
    `deployment`) is `PASS`; exit 1 otherwise.
    """
    quiet = ctx.obj["quiet"]

    report = run_release_check(
        owner=owner,
        repo=repo or service,
        branch=branch,
        pr=pr,
        service=service,
        environment=environment,
        artifact_name=artifact_name,
        token=os.environ.get("GITHUB_TOKEN"),
    )

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "service": report.service,
                    "branch": report.branch,
                    "verdict": report.verdict,
                    "pr": report.pr,
                    "ci": report.ci,
                    "checks": report.checks,
                    "artifacts": report.artifacts,
                    "release": report.release,
                    "deployment": report.deployment,
                    "sources_ok": report.sources_ok,
                    "sources_failed": report.sources_failed,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0 if report.verdict == "ready" else 1)

    if not quiet or report.verdict != "ready":
        typer.echo(f"{report.service} ({report.branch}): {report.verdict.upper()}")
    for label, section in (
        ("pr", report.pr),
        ("ci", report.ci),
        ("checks", report.checks),
        ("artifacts", report.artifacts),
        ("release", report.release),
        ("deployment", report.deployment),
    ):
        typer.echo(f"  [{section['status']}] {label}: {section['detail']}")
    if report.sources_failed:
        typer.echo(f"  unreachable source(s): {', '.join(report.sources_failed)}")
    raise typer.Exit(code=0 if report.verdict == "ready" else 1)


@app.command("observability-inspect")
def observability_inspect(
    ctx: typer.Context,
    service: Annotated[
        str, typer.Argument(help="service name to inspect, e.g. payments")
    ],
    metrics_url: Annotated[
        str,
        typer.Option(
            "--metrics-url",
            help="metrics backend base URL (Prometheus query API shape)",
        ),
    ] = "http://localhost:9090",
    logs_url: Annotated[
        str, typer.Option("--logs-url", help="log-search backend base URL")
    ] = "http://localhost:3100",
    alerts_url: Annotated[
        str,
        typer.Option(
            "--alerts-url", help="alert backend base URL (Alertmanager v2 API shape)"
        ),
    ] = "http://localhost:9093",
    metrics_query: Annotated[
        str | None,
        typer.Option(
            "--metrics-query",
            help="query to run against the metrics backend (defaults to a per-service request-count query)",
        ),
    ] = None,
    correlation_id: Annotated[
        str | None,
        typer.Option(
            "--correlation-id",
            help="reuse an existing correlation ID instead of generating a new one",
        ),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json",
            help="print a machine-readable JSON snapshot instead of plain text",
        ),
    ] = False,
) -> None:
    """Fetch metrics, logs and alerts for one service under one correlation ID, and report what each source found.

    Read-only: this command never writes a metric, never deletes a log, and
    never acknowledges or silences an alert -- it only reads what each
    backend already has. A source this command cannot reach (a timeout, a
    connection refused, an auth failure) is reported as `UNKNOWN` for that
    one section, never silently treated as `EMPTY` -- see
    `platformops.observability` for the fetch/evaluate split that keeps
    "no data found" and "could not check" apart. Every fetch in this run
    happens inside one traced, correlated span -- the correlation ID
    printed below is the same one that appears on every structured log
    line this run wrote; see `platformops.telemetry`. Exit 0 when every
    source answered (whether or not it found data, and whether or not an
    alert is firing); exit 1 when at least one source could not be
    reached.
    """
    quiet = ctx.obj["quiet"]
    resolved_query = metrics_query or f'http_requests_total{{service="{service}"}}'

    snapshot = inspect_observability(
        service=service,
        metrics_base_url=metrics_url,
        metrics_query=resolved_query,
        logs_base_url=logs_url,
        alerts_base_url=alerts_url,
        correlation_id=correlation_id,
        token=os.environ.get("OBSERVABILITY_TOKEN"),
    )

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "service": snapshot.service,
                    "correlation_id": snapshot.correlation_id,
                    "metrics": snapshot.metrics,
                    "logs": snapshot.logs,
                    "alerts": snapshot.alerts,
                    "sources_ok": snapshot.sources_ok,
                    "sources_failed": snapshot.sources_failed,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0 if not snapshot.sources_failed else 1)

    if not quiet or snapshot.sources_failed:
        typer.echo(f"{snapshot.service} (correlation_id={snapshot.correlation_id})")
    for label, section in (
        ("metrics", snapshot.metrics),
        ("logs", snapshot.logs),
        ("alerts", snapshot.alerts),
    ):
        typer.echo(f"  [{section['status']}] {label}: {section['detail']}")
    if snapshot.sources_failed:
        typer.echo(f"  unreachable source(s): {', '.join(snapshot.sources_failed)}")
    raise typer.Exit(code=0 if not snapshot.sources_failed else 1)


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


@app.command("workload-inspect")
def workload_inspect(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Deployment name to inspect, e.g. payments")
    ],
    namespace: Annotated[
        str,
        typer.Option("--namespace", "-n", help="namespace the Deployment lives in"),
    ] = "default",
    kubeconfig: Annotated[
        str | None,
        typer.Option(
            "--kubeconfig",
            help="path to a kubeconfig file (defaults to $KUBECONFIG, then ~/.kube/config)",
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="kubeconfig context to use (defaults to the current context)",
        ),
    ] = None,
    in_cluster: Annotated[
        bool,
        typer.Option(
            "--in-cluster",
            help="authenticate with the in-cluster ServiceAccount token instead of a kubeconfig "
            "(only works when platformops itself is running inside a pod)",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Report a Deployment's replicas, pod status, restarts, image and rollout state, plus recent warning events.

    Read-only: this command never creates, patches, deletes or replaces
    anything in the cluster -- see `platformops.kubernetes_inspect` for the
    same discipline `cloud-audit` already applies to AWS resources. Exit 0
    for a `healthy` rollout, 1 for `degraded`/`unavailable` (a real problem
    with the workload), and 2 for a command-level failure -- the Deployment
    does not exist, the namespace is not readable by this identity, or no
    kubeconfig could be loaded at all.
    """
    quiet = ctx.obj["quiet"]

    try:
        apps_client, core_client = get_kubernetes_clients(
            kubeconfig_path=kubeconfig, context=context, in_cluster=in_cluster
        )
    except ConfigException as exc:
        _fail(str(exc), as_json=as_json, exit_code=2)

    report = inspect_workload(apps_client, core_client, name=name, namespace=namespace)

    if report["status"] == "error":
        _fail(report["message"], as_json=as_json, exit_code=2)

    if as_json:
        typer.echo(json.dumps(report, indent=2))
        raise typer.Exit(code=0 if report["rollout_state"] == "healthy" else 1)

    deployment = report["deployment"]
    if not quiet or report["rollout_state"] != "healthy":
        typer.echo(
            f"{deployment['name']} ({deployment['namespace']}): {report['rollout_state'].upper()} -- "
            f"desired={deployment['desired_replicas']} ready={deployment['ready_replicas']} "
            f"available={deployment['available_replicas']}"
        )
    for pod in report["pods"]:
        typer.echo(f"  pod {pod['name']}: {pod['phase']}")
        for c in pod["containers"]:
            detail = f" -- {c['reason']}" if c["reason"] else ""
            typer.echo(
                f"    {c['name']} ({c['image']}): ready={c['ready']} "
                f"restarts={c['restart_count']} {c['state']}{detail}"
            )
    if report["warning_events"]:
        typer.echo("  warning events:")
        for event in report["warning_events"]:
            typer.echo(
                f"    [{event['involved_object']}] {event['reason']} "
                f"(x{event['count']}): {event['message']}"
            )

    raise typer.Exit(code=0 if report["rollout_state"] == "healthy" else 1)


def _print_restart_plan(plan: RestartPlan, *, as_json: bool) -> None:
    if as_json:
        typer.echo(
            json.dumps({"status": "ok", "plan": restart_plan_to_dict(plan)}, indent=2)
        )
        return
    typer.echo(f"{plan.namespace}/{plan.name} -- {plan.status}")
    if plan.status == "plannable" and plan.action is not None:
        typer.echo(f"  before: {plan.before_rollout_state}")
        typer.echo(f"  would call: {plan.action.api_call} (dry_run=All, validated)")
    if plan.reason:
        typer.echo(f"  reason: {plan.reason}")


def _print_restart_result(result: RestartResult, *, as_json: bool) -> None:
    if as_json:
        typer.echo(
            json.dumps(
                {"status": "ok", "result": restart_result_to_dict(result)}, indent=2
            )
        )
        return
    typer.echo(f"{result.namespace}/{result.name} -- {result.status}")
    if result.rollout_outcome:
        typer.echo(
            f"  rollout: {result.rollout_outcome} (before={result.before_rollout_state} "
            f"after={result.after_rollout_state}, {result.elapsed_seconds}s of "
            f"{result.timeout_seconds}s timeout)"
        )
    if result.message:
        typer.echo(f"  {result.message}")
    if result.rollout_outcome == "timed_out":
        typer.echo(
            "  rollback guidance: this restart did not fix the problem -- run "
            "`workload-inspect` to see the current failure reason, and "
            "`kubectl rollout undo deployment/<name> -n <namespace>` to return to the "
            "previous, working template if this made things worse"
        )


def _print_restart_refused(exc: RestartNotApprovedError, *, as_json: bool) -> None:
    """Print the plan an un-approved restart-execute call was about to refuse -- same shape either mode."""
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "plan": restart_plan_to_dict(exc.plan),
                },
                indent=2,
            )
        )
    else:
        _print_restart_plan(exc.plan, as_json=False)
        typer.echo(f"Error: {exc}", err=True)


def _print_restart_cooling_down(exc: RestartCoolingDownError, *, as_json: bool) -> None:
    """Print the cooldown refusal -- the runaway-restart-loop guard, not an error in the plan itself."""
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "error",
                    "error": str(exc),
                    "seconds_remaining": round(exc.seconds_remaining, 1),
                    "plan": restart_plan_to_dict(exc.plan),
                },
                indent=2,
            )
        )
    else:
        typer.echo(f"Error: {exc}", err=True)


@app.command("restart-plan")
def restart_plan_cmd(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Deployment name to restart, e.g. checkout-web")
    ],
    namespace: Annotated[
        str,
        typer.Option("--namespace", "-n", help="namespace the Deployment lives in"),
    ] = "default",
    ops_config: Annotated[
        Path,
        typer.Option(
            "--ops-config",
            help="path to a governed-operations allowlist/policy YAML file",
        ),
    ] = Path("k8s-ops.example.yaml"),
    kubeconfig: Annotated[
        str | None,
        typer.Option(
            "--kubeconfig",
            help="path to a kubeconfig file (defaults to $KUBECONFIG, then ~/.kube/config)",
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="kubeconfig context to use (defaults to the current context)",
        ),
    ] = None,
    in_cluster: Annotated[
        bool,
        typer.Option(
            "--in-cluster",
            help="authenticate with the in-cluster ServiceAccount token instead of a kubeconfig",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Show what restarting one Deployment would do -- validated with a real server-side dry run, never restarts for real.

    This command never mutates the cluster. It checks the namespace against
    the ops config's `namespace_allowlist` first -- refusing regardless of
    whether the Deployment even needs a restart -- then validates the exact
    restart patch with Kubernetes' own `dry_run="All"`: the API server
    confirms the patch would be accepted, without persisting it. Nothing
    here is a substitute for `restart-execute --approve`.
    """
    config = load_ops_config(ops_config)
    try:
        apps_client, _core_client = get_kubernetes_clients(
            kubeconfig_path=kubeconfig, context=context, in_cluster=in_cluster
        )
    except ConfigException as exc:
        _fail(str(exc), as_json=as_json, exit_code=2)

    plan = build_restart_plan(
        apps_client,
        namespace=namespace,
        name=name,
        allowlist=config["namespace_allowlist"],
    )

    _print_restart_plan(plan, as_json=as_json)
    raise typer.Exit(code=0 if plan.status == "plannable" else 1)


@app.command("restart-execute")
def restart_execute_cmd(
    ctx: typer.Context,
    name: Annotated[
        str, typer.Argument(help="Deployment name to restart, e.g. checkout-web")
    ],
    namespace: Annotated[
        str,
        typer.Option("--namespace", "-n", help="namespace the Deployment lives in"),
    ] = "default",
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            help="required to actually restart anything -- omit it to see the plan and exit non-zero",
        ),
    ] = False,
    ops_config: Annotated[
        Path,
        typer.Option(
            "--ops-config",
            help="path to a governed-operations allowlist/policy YAML file",
        ),
    ] = Path("k8s-ops.example.yaml"),
    audit_log: Annotated[
        Path | None,
        typer.Option(
            "--audit-log",
            help="JSON-lines file every real restart is appended to "
            "(defaults to the ops config's audit_log_path)",
        ),
    ] = None,
    kubeconfig: Annotated[
        str | None,
        typer.Option(
            "--kubeconfig",
            help="path to a kubeconfig file (defaults to $KUBECONFIG, then ~/.kube/config)",
        ),
    ] = None,
    context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="kubeconfig context to use (defaults to the current context)",
        ),
    ] = None,
    in_cluster: Annotated[
        bool,
        typer.Option(
            "--in-cluster",
            help="authenticate with the in-cluster ServiceAccount token instead of a kubeconfig",
        ),
    ] = False,
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Restart one Deployment -- refuses to restart anything without --approve.

    Without `--approve`, this command prints the exact plan it would have
    run and exits non-zero. With `--approve`, it is refused a second way if
    the SAME Deployment was restarted inside the ops config's
    `cooldown_seconds` window -- the guard against a runaway restart loop.
    Past both gates, it re-validates the restart with a fresh server-side
    dry run immediately before mutating, patches the Deployment's pod
    template (the same mechanism `kubectl rollout restart` uses), and polls
    the rollout for up to `rollout_timeout_seconds`. Every real restart is
    appended to `--audit-log`, whether the rollout completed in time or not.
    """
    config = load_ops_config(ops_config)
    resolved_audit_log = (
        audit_log if audit_log is not None else Path(config["audit_log_path"])
    )
    try:
        apps_client, _core_client = get_kubernetes_clients(
            kubeconfig_path=kubeconfig, context=context, in_cluster=in_cluster
        )
    except ConfigException as exc:
        _fail(str(exc), as_json=as_json, exit_code=2)

    plan = build_restart_plan(
        apps_client,
        namespace=namespace,
        name=name,
        allowlist=config["namespace_allowlist"],
    )

    try:
        result = execute_restart(
            apps_client,
            plan,
            approve=approve,
            allowlist=config["namespace_allowlist"],
            cooldown_seconds=config["cooldown_seconds"],
            rollout_timeout_seconds=config["rollout_timeout_seconds"],
            rollout_poll_interval_seconds=config["rollout_poll_interval_seconds"],
            audit_log_path=resolved_audit_log,
            actor=os.environ.get("USER", "cli-operator"),
        )
    except RestartNotApprovedError as exc:
        _print_restart_refused(exc, as_json=as_json)
        raise typer.Exit(code=1) from None
    except RestartCoolingDownError as exc:
        _print_restart_cooling_down(exc, as_json=as_json)
        raise typer.Exit(code=1) from None

    _print_restart_result(result, as_json=as_json)
    ok = result.status == "restarted" and result.rollout_outcome == "rolled_out"
    raise typer.Exit(code=0 if ok else 1)


def _report_to_dict(report: IncidentContextReport) -> dict[str, Any]:
    return {
        "status": "ok" if not report.sources_failed else "partial",
        "service": report.service,
        "ownership": report.ownership,
        "source_changes": report.source_changes,
        "ci": report.ci,
        "kubernetes": report.kubernetes,
        "cloud": report.cloud,
        "observability": report.observability,
        "runbook": report.runbook,
        "timeline": report.timeline,
        "sources_ok": report.sources_ok,
        "sources_failed": report.sources_failed,
    }


@app.command("incident-collect")
def incident_collect(
    ctx: typer.Context,
    service: Annotated[
        str,
        typer.Argument(
            help="service name to gather incident context for, e.g. payments"
        ),
    ],
    owner: Annotated[
        str,
        typer.Option(
            "--owner", help="GitHub organization or user that owns the repository"
        ),
    ],
    service_path: Annotated[
        Path,
        typer.Option(
            "--service-path", help="path to the service's service definition YAML file"
        ),
    ] = Path("service.yaml"),
    repo_path: Annotated[
        str,
        typer.Option(
            "--repo-path",
            help="local path to check with git for source-control evidence",
        ),
    ] = ".",
    repo: Annotated[
        str | None,
        typer.Option(
            "--repo", help="repository name, if different from the service name"
        ),
    ] = None,
    branch: Annotated[
        str | None,
        typer.Option(
            "--branch",
            help="only count workflow run(s) on this branch (omit to see the most "
            "recent run on any branch)",
        ),
    ] = None,
    namespace: Annotated[
        str,
        typer.Option("--namespace", "-n", help="namespace the Deployment lives in"),
    ] = "default",
    deployment_name: Annotated[
        str | None,
        typer.Option(
            "--deployment-name",
            help="Deployment name, if different from the service name",
        ),
    ] = None,
    kubeconfig: Annotated[
        str | None,
        typer.Option(
            "--kubeconfig",
            help="path to a kubeconfig file (defaults to $KUBECONFIG, then ~/.kube/config)",
        ),
    ] = None,
    kube_context: Annotated[
        str | None,
        typer.Option(
            "--context",
            help="kubeconfig context to use (defaults to the current context)",
        ),
    ] = None,
    in_cluster: Annotated[
        bool,
        typer.Option(
            "--in-cluster",
            help="authenticate with the in-cluster ServiceAccount token instead of a kubeconfig",
        ),
    ] = False,
    policy: Annotated[
        Path, typer.Option("--policy", help="path to a cloud policy YAML file")
    ] = Path("policy.example.yaml"),
    region: Annotated[
        str, typer.Option("--region", help="AWS region to check for cloud evidence")
    ] = "us-east-1",
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
    metrics_url: Annotated[
        str,
        typer.Option(
            "--metrics-url",
            help="metrics backend base URL (Prometheus query API shape)",
        ),
    ] = "http://localhost:9090",
    logs_url: Annotated[
        str, typer.Option("--logs-url", help="log-search backend base URL")
    ] = "http://localhost:3100",
    alerts_url: Annotated[
        str,
        typer.Option(
            "--alerts-url", help="alert backend base URL (Alertmanager v2 API shape)"
        ),
    ] = "http://localhost:9093",
    metrics_query: Annotated[
        str | None,
        typer.Option(
            "--metrics-query",
            help="query to run against the metrics backend (defaults to a "
            "per-service request-count query)",
        ),
    ] = None,
    correlation_id: Annotated[
        str | None,
        typer.Option(
            "--correlation-id",
            help="reuse an existing correlation ID instead of generating a new one",
        ),
    ] = None,
    registry: Annotated[
        Path,
        typer.Option(
            "--registry",
            help="path to the incident registry YAML file (runbook URL + SLO target)",
        ),
    ] = Path("incident.example.yaml"),
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Gather one incident's context -- ownership, recent changes, CI state, Kubernetes health, cloud state, observability and a runbook -- into one report.

    Read-only across every source it touches: this command never restarts a
    workload, never remediates a cloud finding, never mutates anything --
    see `platformops.incidentcontext` for the six adapters it composes and
    the Deep Dive's grep proof. A source this run cannot reach (kind not
    running, Floci down, GitHub unreachable, a metrics backend refusing the
    connection) is reported as `UNKNOWN` for that one section and named in
    `sources_failed`; every other section that did answer still appears in
    the report. Exit 0 when every source answered (whatever it found);
    exit 1 when at least one source could not be reached at all.
    """
    quiet = ctx.obj["quiet"]
    resolved_repo = repo or service
    resolved_deployment_name = deployment_name or service
    resolved_metrics_query = (
        metrics_query or f'http_requests_total{{service="{service}"}}'
    )

    report = collect_incident_context(
        service=service,
        service_path=service_path,
        repo_path=repo_path,
        owner=owner,
        repo=resolved_repo,
        branch=branch,
        namespace=namespace,
        deployment_name=resolved_deployment_name,
        kubeconfig_path=kubeconfig,
        context=kube_context,
        in_cluster=in_cluster,
        policy_path=policy,
        region=region,
        profile=profile,
        endpoint_url=endpoint_url,
        metrics_base_url=metrics_url,
        metrics_query=resolved_metrics_query,
        logs_base_url=logs_url,
        alerts_base_url=alerts_url,
        correlation_id=correlation_id,
        registry_path=registry,
        github_token=os.environ.get("GITHUB_TOKEN"),
        observability_token=os.environ.get("OBSERVABILITY_TOKEN"),
    )

    if as_json:
        typer.echo(json.dumps(_report_to_dict(report), indent=2))
        raise typer.Exit(code=0 if not report.sources_failed else 1)

    if not quiet or report.sources_failed:
        typer.echo(f"{report.service}")
    for label, section in (
        ("ownership", report.ownership),
        ("source", report.source_changes),
        ("ci", report.ci),
        ("kubernetes", report.kubernetes),
        ("cloud", report.cloud),
        ("observability", report.observability),
        ("runbook", report.runbook),
    ):
        typer.echo(f"  [{section['status']}] {label}: {section['detail']}")

    if report.timeline:
        typer.echo("timeline:")
        for entry in report.timeline:
            typer.echo(
                f"  {entry['timestamp']}  {entry['source']:<10}  {entry['summary']}"
            )

    if report.sources_failed:
        typer.echo(f"  unreachable source(s): {', '.join(report.sources_failed)}")

    raise typer.Exit(code=0 if not report.sources_failed else 1)


@app.command("ai-inspect")
def ai_inspect_cmd(
    ctx: typer.Context,
    path: Annotated[
        Path, typer.Argument(help="path to an AI service definition YAML file")
    ],
    mlflow_url: Annotated[
        str,
        typer.Option(
            "--mlflow-url",
            help="MLflow tracking server base URL, e.g. http://localhost:5000",
        ),
    ] = "http://localhost:5000",
    as_json: Annotated[
        bool,
        typer.Option(
            "--json", help="print a machine-readable JSON report instead of plain text"
        ),
    ] = False,
) -> None:
    """Report a model version's registry state, its training run's outcome, and its serving endpoint's health.

    Read-only: this command never registers a model version, never
    transitions a stage, and never restarts a serving process -- see
    `platformops.ai_inspect` for the fetch/aggregate split, and its Deep
    Dive for the mechanical proof. An `online` service's endpoint is
    health-checked with the same `check_health()` this toolkit already
    uses (Module 9); a `batch` service's endpoint section reports
    `NOT_APPLICABLE` -- a batch job with no standing endpoint is not "down".
    Exit 0 for `healthy`, 1 for `unhealthy`, 2 when the file itself is not a
    valid AI service definition.
    """
    quiet = ctx.obj["quiet"]

    try:
        data = load_yaml_dict(path)
    except ConfigError as exc:
        _fail(str(exc), as_json=as_json, exit_code=2)

    result = validate_ai_service(data)
    if isinstance(result, list):
        _fail(
            f"{path} is not a valid AI service definition -- {len(result)} field(s) failed",
            as_json=as_json,
            exit_code=2,
        )
        # unreachable -- _fail() always raises; this line only narrows `result` for mypy
        raise typer.Exit(code=2)

    report = inspect_ai_workload(
        result,
        mlflow_base_url=mlflow_url,
        token=os.environ.get("MLFLOW_TRACKING_TOKEN"),
    )

    if as_json:
        typer.echo(
            json.dumps(
                {
                    "status": "ok",
                    "service": report.service,
                    "inference_mode": report.inference_mode,
                    "registered_model_name": report.registered_model_name,
                    "model_version": report.model_version,
                    "serving_runtime": report.serving_runtime,
                    "verdict": report.verdict,
                    "model": report.model,
                    "run": report.run,
                    "endpoint": report.endpoint,
                    "sources_ok": report.sources_ok,
                    "sources_failed": report.sources_failed,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=0 if report.verdict == "healthy" else 1)

    if not quiet or report.verdict != "healthy":
        typer.echo(
            f"{report.service} ({report.inference_mode}, "
            f"{report.registered_model_name}:{report.model_version} on "
            f"{report.serving_runtime}): {report.verdict.upper()}"
        )
    for label, section in (
        ("model", report.model),
        ("run", report.run),
        ("endpoint", report.endpoint),
    ):
        typer.echo(f"  [{section['status']}] {label}: {section['detail']}")
    if report.sources_failed:
        typer.echo(f"  unreachable source(s): {', '.join(report.sources_failed)}")
    raise typer.Exit(code=0 if report.verdict == "healthy" else 1)


@app.command("api-serve")
def api_serve(
    host: Annotated[
        str, typer.Option("--host", help="interface to bind the API server to")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", help="port to bind the API server to")
    ] = 8080,
    api_keys: Annotated[
        Path,
        typer.Option(
            "--api-keys",
            help="path to an api-keys YAML file mapping X-API-Key values to caller names",
        ),
    ] = Path("api-keys.example.yaml"),
    audit_log: Annotated[
        Path,
        typer.Option(
            "--audit-log",
            help="JSON-lines file every API request (successful or not) is appended to",
        ),
    ] = Path("api-audit.jsonl"),
) -> None:
    """Serve the PlatformOps internal API -- POST /services/validate, GET /health,
    GET /release-readiness, GET /incident-context, POST /remediations/plan.

    Every route (except /health) requires a valid `X-API-Key` header, checked
    against `--api-keys`. Every route handler is a thin wrapper that calls
    the same function the matching CLI command already calls -- see
    `platformops.api` for the full reuse contract. There is no
    `/remediations/execute` route: this command never mutates anything a
    caller could reach over the network.
    """
    import uvicorn

    from platformops.api import create_app, load_api_key_config

    keys = load_api_key_config(api_keys)
    application = create_app(api_keys=keys, audit_log_path=audit_log)
    uvicorn.run(application, host=host, port=port)


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
