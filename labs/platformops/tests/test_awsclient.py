from platformops import awsclient

# ---------------------------------------------------------------------------
# get_aws_client -- session, profile, region and retry config, the same
# wiring cloudinventory.get_client() proved for EC2 alone, generalized to
# any service name. boto3.Session is faked so these tests prove the
# arguments this function passes, not real network behavior.
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, profile_name=None):
        self.profile_name = profile_name
        self.calls = []

    def client(self, service_name, *, region_name=None, endpoint_url=None, config=None):
        self.calls.append(
            {
                "service_name": service_name,
                "region_name": region_name,
                "endpoint_url": endpoint_url,
                "config": config,
            }
        )
        return f"fake-{service_name}-client"


def _install_fake_session(monkeypatch):
    fake = FakeSession.__new__(FakeSession)

    def factory(profile_name=None):
        fake.__init__(profile_name=profile_name)
        return fake

    monkeypatch.setattr(awsclient.boto3, "Session", factory)
    return fake


def test_passes_service_name_region_and_profile(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    client = awsclient.get_aws_client("s3", region="us-east-1", profile="course-lab")

    assert client == "fake-s3-client"
    assert fake.profile_name == "course-lab"
    call = fake.calls[0]
    assert call["service_name"] == "s3"
    assert call["region_name"] == "us-east-1"


def test_uses_retry_config(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    awsclient.get_aws_client("sqs", region="us-east-1")

    assert fake.calls[0]["config"].retries == {"max_attempts": 5, "mode": "standard"}


def test_endpoint_url_is_none_by_default_so_it_talks_to_real_aws(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    awsclient.get_aws_client("dynamodb", region="us-east-1")

    assert fake.calls[0]["endpoint_url"] is None


def test_explicit_endpoint_url_parameter_is_used(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)

    awsclient.get_aws_client(
        "s3", region="us-east-1", endpoint_url="http://localhost:4566"
    )

    assert fake.calls[0]["endpoint_url"] == "http://localhost:4566"


def test_endpoint_url_falls_back_to_env_var_when_not_passed(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")

    awsclient.get_aws_client("dynamodb", region="us-east-1")

    assert fake.calls[0]["endpoint_url"] == "http://localhost:4566"


def test_explicit_endpoint_url_parameter_overrides_env_var(monkeypatch):
    fake = _install_fake_session(monkeypatch)
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://localhost:4566")

    awsclient.get_aws_client(
        "s3", region="us-east-1", endpoint_url="http://localhost:9999"
    )

    assert fake.calls[0]["endpoint_url"] == "http://localhost:9999"
