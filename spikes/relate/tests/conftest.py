import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import cleanup, fresh_store  # noqa: E402


@pytest.fixture
def store():
    p = fresh_store("t")
    yield p
    cleanup(p)
