import nexus_context_service


def test_package_imports() -> None:
    assert nexus_context_service.__version__ == "0.1.0"
