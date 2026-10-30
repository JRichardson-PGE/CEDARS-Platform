import pytest


@pytest.fixture
def sandbox(settings):
    settings.ENVIRONMENT = "sandbox"
