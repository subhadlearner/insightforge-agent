import pytest


@pytest.fixture(autouse=True)
def clean_env():
    """Live tests use the real environment and .env; override the isolating fixture."""
