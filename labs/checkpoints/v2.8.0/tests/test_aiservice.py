import pytest

from platformops.aiservice import (
    GOOD_AI_EXAMPLE,
    AIServiceDefinition,
    validate_ai_service,
)


def test_a_complete_valid_ai_service_definition_validates_successfully():
    result = validate_ai_service(GOOD_AI_EXAMPLE)
    assert isinstance(result, AIServiceDefinition)
    assert result.name == "support-assistant"
    assert result.inference_mode == "online"
    assert result.registered_model_name == "support-assistant-intent-classifier"


def test_a_missing_required_field_fails_with_the_field_required_error():
    data = {key: value for key, value in GOOD_AI_EXAMPLE.items() if key != "endpoint"}

    errors = validate_ai_service(data)

    assert isinstance(errors, list)
    assert any(
        error["loc"] == ("endpoint",) and error["type"] == "missing" for error in errors
    )


def test_an_invalid_inference_mode_is_rejected_not_silently_accepted():
    data = {**GOOD_AI_EXAMPLE, "inference_mode": "streaming"}

    errors = validate_ai_service(data)

    assert isinstance(errors, list)
    assert any(error["loc"] == ("inference_mode",) for error in errors)


@pytest.mark.parametrize("bad_version", ["v4", "4.0", "latest", ""])
def test_a_non_numeric_model_version_is_rejected(bad_version):
    data = {**GOOD_AI_EXAMPLE, "model_version": bad_version}

    errors = validate_ai_service(data)

    assert isinstance(errors, list)
    assert any(error["loc"] == ("model_version",) for error in errors)


def test_batch_inference_mode_is_accepted_with_no_extra_fields_required():
    data = {**GOOD_AI_EXAMPLE, "inference_mode": "batch"}

    result = validate_ai_service(data)

    assert isinstance(result, AIServiceDefinition)
    assert result.inference_mode == "batch"


def test_to_summary_reads_as_one_line_naming_the_model_version_and_runtime():
    result = validate_ai_service(GOOD_AI_EXAMPLE)
    assert isinstance(result, AIServiceDefinition)

    summary = result.to_summary()

    assert "support-assistant" in summary
    assert "support-assistant-intent-classifier:4" in summary
    assert "vllm" in summary
