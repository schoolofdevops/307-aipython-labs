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


def test_an_aws_account_that_is_not_exactly_12_digits_is_rejected():
    # Right type (a string), wrong value -- the schema catches this by
    # pattern, not by type, so a truncated account number is never silently
    # accepted just because it happens to be a string.
    data = {**GOOD_EXAMPLE, "aws_account": "12345"}
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(error["loc"] == ("aws_account",) for error in errors)


def test_an_environment_outside_dev_staging_prod_is_rejected():
    data = {**GOOD_EXAMPLE, "environment": "production"}
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(error["loc"] == ("environment",) for error in errors)


def test_a_region_outside_the_allowed_list_is_rejected():
    data = {**GOOD_EXAMPLE, "region": "ap-northeast-9"}
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(error["loc"] == ("region",) for error in errors)


def test_a_namespace_that_is_not_lowercase_dns_safe_is_rejected():
    data = {**GOOD_EXAMPLE, "kubernetes_namespace": "Checkout_API"}
    errors = validate_service(data)
    assert isinstance(errors, list)
    assert any(error["loc"] == ("kubernetes_namespace",) for error in errors)


def test_to_summary_reads_as_one_line_for_a_paging_alert():
    result = validate_service(GOOD_EXAMPLE)
    assert isinstance(result, ServiceDefinition)
    summary = result.to_summary()
    assert "checkout-api" in summary
    assert "prod/us-east-1" in summary
    assert "payments-team" in summary
