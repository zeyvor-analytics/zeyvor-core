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

    def _profile(name: str, options: ProfileOptions | None = None, *, as_name: str | None = None):
        resolved = resolve_source(fixture_path(name))
        try:
            profile = Profiler(resolved.engine, options).profile(resolved.relation)
        finally:
            resolved.close()
        if as_name:
            # Lets a contract generated from one fixture be checked against
            # another, standing in for "the same table, one week later".
            profile.name = as_name
        return profile

    return _profile


@pytest.fixture
def baseline_contract(profile_fixture):
    """A contract generated from a fixture, as `zeyvor init` would produce it.

    Column sets differ between fixtures, so missing columns are tolerated —
    these pairs stand in for one table changing over time, not for a schema
    comparison.
    """
    from zeyvor.contract import generate_contract

    def _contract(name: str, *, table_name: str | None = None, tolerant: bool = True):
        profile = profile_fixture(name, as_name=table_name)
        contract = generate_contract(profile)
        if tolerant:
            for table in contract.tables.values():
                table.allow_missing_columns = True
        return contract

    return _contract
