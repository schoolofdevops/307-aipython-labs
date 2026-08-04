"""Service definition model -- PlatformOps v0.3.

A service definition is the small record every team is supposed to keep up to
date about their own service: what it is called, where its code lives, which
environment and namespace it runs in, who owns it, and where to look when it
misbehaves. Today that record usually lives in a YAML file a team edits by
hand -- which means it is exactly as trustworthy as the last person who
edited it.

`ServiceDefinition` turns a raw dict (loaded from that YAML, or from an API
call, or typed in by a user) into a validated, typed object. `validate_service`
is the one function anything in this toolkit calls to make that conversion --
nothing downstream should ever read a raw service dict again.
"""

from typing import Literal

from pydantic import BaseModel, Field, ValidationError

Environment = Literal["dev", "staging", "prod"]
Region = Literal["us-east-1", "us-west-2", "eu-west-1", "ap-south-1"]

# RFC 1123 DNS label: lowercase letters, digits, hyphens; must not start or end
# with a hyphen. Kubernetes rejects anything else for a namespace name, so
# rejecting it here is catching the same mistake before it reaches a cluster.
_NAMESPACE_PATTERN = r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"

# An AWS account ID is always exactly 12 digits. Nothing about `str` alone
# catches a truncated or transposed account number -- that is a value the
# type system waves through, so the pattern below has to do it instead.
_AWS_ACCOUNT_PATTERN = r"^\d{12}$"


class ObservabilityRefs(BaseModel):
    """Where an on-call engineer looks when this service misbehaves."""

    dashboard_url: str
    alert_channel: str


class ServiceDefinition(BaseModel):
    """One service's operational identity."""

    name: str
    repository: str
    environment: Environment
    team_owner: str
    kubernetes_namespace: str = Field(pattern=_NAMESPACE_PATTERN)
    deployment_name: str
    aws_account: str = Field(pattern=_AWS_ACCOUNT_PATTERN)
    region: Region
    observability: ObservabilityRefs

    def to_summary(self) -> str:
        """One line an on-call engineer can scan in a paging alert."""
        return (
            f"{self.name} [{self.environment}/{self.region}] "
            f"ns={self.kubernetes_namespace} owner={self.team_owner} "
            f"alerts={self.observability.alert_channel}"
        )


def validate_service(data: dict) -> ServiceDefinition | list[dict]:
    """Validate a raw dict against `ServiceDefinition`.

    Returns the validated model on success. On failure, returns
    `ValidationError.errors()` -- a plain list of dicts -- instead of raising,
    so a caller can log, report or reject bad input without wrapping every
    call site in its own try/except.
    """
    try:
        return ServiceDefinition.model_validate(data)
    except ValidationError as exc:
        return exc.errors()


GOOD_EXAMPLE = {
    "name": "checkout-api",
    "repository": "github.com/example/checkout-api",
    "environment": "prod",
    "team_owner": "payments-team",
    "kubernetes_namespace": "checkout",
    "deployment_name": "checkout-api",
    "aws_account": "111122223333",
    "region": "us-east-1",
    "observability": {
        "dashboard_url": "https://grafana.example.com/d/checkout-api",
        "alert_channel": "#checkout-alerts",
    },
}

BAD_EXAMPLE = {
    "name": "checkout-api",
    "repository": "github.com/example/checkout-api",
    "environment": "prod",
    "team_owner": "payments-team",
    "kubernetes_namespace": "checkout",
    "aws_account": "111122223333",
    "region": "us-east-1",
    "observability": {
        "dashboard_url": "https://grafana.example.com/d/checkout-api",
        "alert_channel": "#checkout-alerts",
    },
}


def _print_result(label: str, data: dict) -> None:
    result = validate_service(data)
    print(f"{label}:")
    if isinstance(result, ServiceDefinition):
        print(
            f"  OK -- {result.name} ({result.environment}/{result.kubernetes_namespace})"
        )
    else:
        for error in result:
            location = ".".join(str(part) for part in error["loc"])
            print(f"  FAIL -- {location}: {error['msg']}")


if __name__ == "__main__":
    _print_result("Good service definition", GOOD_EXAMPLE)
    print()
    _print_result("Bad service definition (missing deployment_name)", BAD_EXAMPLE)
