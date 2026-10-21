import os
from pathlib import Path

import pytest

ALLOW_ENV = "INSIGHTFORGE_ALLOW_LIVE_TESTS"
_HERE = Path(__file__).parent


def live_allowed(environ=os.environ) -> bool:
    """Live tests make real, paid provider calls from your own environment and .env. Selecting
    them with `-m live` is not enough: this second, explicit opt-in is also required."""
    return environ.get(ALLOW_ENV) == "1"


def pytest_collection_modifyitems(items):
    """Skip every live test unless authorised. A skip mark is applied before any fixture runs,
    so no provider is reached (module fixtures such as the spike outcome never start)."""
    if live_allowed():
        return
    skip = pytest.mark.skip(reason=f"live tests need {ALLOW_ENV}=1 as well as -m live "
                                   "(they make real, paid provider calls)")
    for item in items:
        if _HERE in Path(str(item.path)).parents:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def clean_env():
    """Live tests use the real environment and .env; override the isolating fixture."""
