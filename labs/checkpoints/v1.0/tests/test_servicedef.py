import pytest

from platformops.servicedef import GOOD_EXAMPLE, ServiceDefinition, validate_service


def test_a_complete_valid_definition_validates_successfully():
    result = validate_service(GOOD_EXAMPLE)
    assert isinstance(result, ServiceDefinition)
    assert result.name == "checkout-api"
    assert result.observability.alert_channel == "#checkout-alerts"


def test_a_missing_required_field_fails_with_the_field_required_error():
    data = {
        key: value for key, value in GOOD_EXAMPLE.items() if key != "deployment_name"
    }
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(
        error["loc"] == ("deployment_name",) and error["type"] == "missing"
        for error in errors
    )


def test_a_wrong_type_value_fails_instead_of_being_silently_accepted():
    data = {**GOOD_EXAMPLE, "aws_account": 111122223333}  # int, not str
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(
        error["loc"] == ("aws_account",) and error["type"] == "string_type"
        for error in errors
    )


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("aws_account", "12345"),
        ("environment", "production"),
        ("region", "ap-northeast-9"),
        ("kubernetes_namespace", "Checkout_API"),
    ],
)
def test_a_bad_field_value_is_rejected(field, bad_value):
    data = {**GOOD_EXAMPLE, field: bad_value}

    errors = validate_service(data)

    assert isinstance(errors, list)
    assert any(error["loc"] == (field,) for error in errors)


def test_to_summary_reads_as_one_line_for_a_paging_alert():
    result = validate_service(GOOD_EXAMPLE)
    assert isinstance(result, ServiceDefinition)
    summary = result.to_summary()
    assert "checkout-api" in summary
    assert "prod/us-east-1" in summary
    assert "payments-team" in summary
