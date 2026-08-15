"""AI service definitions -- PlatformOps v2.8.

`servicedef.py` (Module 5) answers one question about a normal web service:
what does its operational identity look like -- where does it run, who
owns it, where do the alerts go. An AI-serving workload -- a model behind
an inference endpoint -- carries the same ownership questions, plus a set
that a plain web service never needs answered: which model, which version
of it, trained and tracked by which run, served by which runtime, and
reachable how (a live endpoint that answers one request at a time, or a
batch job with no endpoint to poll at all).

Think of it the way a pharmacy tracks a batch of medicine, not just the
shelf it sits on. A shelf label ("aisle 4, cold storage") is what
`ServiceDefinition` already captures -- where the thing lives. A medicine
label also carries a batch number and an expiry date -- which exact batch
this is, and whether it is still the one that passed inspection. A model
version is that batch number: `support-assistant-intent-classifier:4` is
not the same as `:5`, even if both currently answer requests at the same
URL, the same way two batches of the same drug are not interchangeable
just because they sit on the same shelf.

`AIServiceDefinition` is a new, separate model rather than new fields
bolted onto `ServiceDefinition` -- the same choice `cloudaudit.py` (Module
24) made when it wrapped `ResourceRecord` instead of extending it. A plain
web service will never have a `model_version` or a `serving_runtime`; giving
`ServiceDefinition` those fields would mean every existing service record
in this project carries fields that are always empty. `validate_ai_service()`
mirrors `validate_service()` exactly: it returns the validated model on
success, or Pydantic's own list of error dicts on failure -- never raises,
so a caller reports every problem in one file instead of stopping at the
first one.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from platformops.servicedef import ObservabilityRefs

InferenceMode = Literal["batch", "online"]

# MLflow's own model version identifier is a plain, ever-increasing integer
# rendered as a string ("1", "2", "3", ...) -- never "v3" or "latest". Only
# an *exact* version reaches this toolkit: an inspector that let "latest"
# through would be inspecting whatever the registry happens to point at
# right now, not the version a deployment record actually claims to run.
_MODEL_VERSION_PATTERN = r"^\d+$"


class AIServiceDefinition(BaseModel):
    """One AI-serving workload's operational identity: the model behind it, not just where it runs."""

    name: str
    team_owner: str
    model_registry: str
    registered_model_name: str
    model_version: str = Field(pattern=_MODEL_VERSION_PATTERN)
    serving_runtime: str
    endpoint: str
    inference_mode: InferenceMode
    observability: ObservabilityRefs

    def to_summary(self) -> str:
        """One line an on-call engineer can scan -- names the exact model version, not just the service."""
        return (
            f"{self.name} [{self.inference_mode}] "
            f"model={self.registered_model_name}:{self.model_version} "
            f"runtime={self.serving_runtime} owner={self.team_owner}"
        )


def validate_ai_service(
    data: dict[str, Any],
) -> AIServiceDefinition | list[dict[str, Any]]:
    """Validate a raw dict against `AIServiceDefinition`.

    Same contract as `platformops.servicedef.validate_service()`: the
    validated model on success, or `ValidationError.errors()` -- a plain
    list of dicts -- on failure, so a caller never has to wrap this in its
    own try/except to report every problem at once.
    """
    try:
        return AIServiceDefinition.model_validate(data)
    except ValidationError as exc:
        return [dict(error) for error in exc.errors()]


GOOD_AI_EXAMPLE = {
    "name": "support-assistant",
    "team_owner": "ml-platform-team",
    "model_registry": "mlflow",
    "registered_model_name": "support-assistant-intent-classifier",
    "model_version": "4",
    "serving_runtime": "vllm",
    "endpoint": "https://support-assistant.internal.example.com/v1/predict",
    "inference_mode": "online",
    "observability": {
        "dashboard_url": "https://grafana.example.com/d/support-assistant",
        "alert_channel": "#support-assistant-alerts",
    },
}

BAD_AI_EXAMPLE = {
    "name": "support-assistant",
    "team_owner": "ml-platform-team",
    "model_registry": "mlflow",
    "registered_model_name": "support-assistant-intent-classifier",
    "serving_runtime": "vllm",
    "endpoint": "https://support-assistant.internal.example.com/v1/predict",
    "inference_mode": "online",
    "observability": {
        "dashboard_url": "https://grafana.example.com/d/support-assistant",
        "alert_channel": "#support-assistant-alerts",
    },
}


def _print_result(label: str, data: dict) -> None:
    result = validate_ai_service(data)
    print(f"{label}:")
    if isinstance(result, AIServiceDefinition):
        print(f"  OK -- {result.to_summary()}")
    else:
        for error in result:
            location = ".".join(str(part) for part in error["loc"])
            print(f"  FAIL -- {location}: {error['msg']}")


if __name__ == "__main__":
    _print_result("Good AI service definition", GOOD_AI_EXAMPLE)
    print()
    _print_result("Bad AI service definition (missing model_version)", BAD_AI_EXAMPLE)
