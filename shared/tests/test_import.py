import nexus_shared


def test_package_imports() -> None:
    assert nexus_shared.__version__ == "0.1.0"
