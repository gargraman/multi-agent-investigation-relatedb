import nexus_contracts


def test_package_imports() -> None:
    assert nexus_contracts.__version__ == "0.1.0"
