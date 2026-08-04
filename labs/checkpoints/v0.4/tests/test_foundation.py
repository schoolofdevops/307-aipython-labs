from platformops import __version__, about


def test_version_is_pinned_to_v0_0():
    assert __version__ == "0.0.0"


def test_about_returns_the_toolkit_tagline():
    message = about()
    assert "PlatformOps Toolkit" in message
    assert __version__ in message
