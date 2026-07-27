from __future__ import annotations

import pytest

from helpers import fixture_path
from zeyvor import DuckDBEngine
from zeyvor.profile import ProfileOptions, Profiler
from zeyvor.sources import resolve_source


@pytest.fixture(scope="session")
def duck():
    engine = DuckDBEngine()
    yield engine
    engine.close()


@pytest.fixture
def profile_fixture():
    """Profile a fixture CSV and return the TableProfile."""

    def _profile(name: str, options: ProfileOptions | None = None):
        resolved = resolve_source(fixture_path(name))
        try:
            return Profiler(resolved.engine, options).profile(resolved.relation)
        finally:
            resolved.close()

    return _profile
