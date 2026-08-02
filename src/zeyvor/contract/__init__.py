"""Contracts: what the data is supposed to mean, and whether it still does.

    from zeyvor import profile_source
    from zeyvor.contract import generate_contract, check, dump, load

    baseline = profile_source("orders.csv")
    contract = generate_contract(baseline)          # add describer= for prose
    dump(contract, "zeyvor.yml")

    # later, in CI
    report = check(profile_source("orders.csv"), load("zeyvor.yml"))
    print(report.render())
    raise SystemExit(report.exit_code)

Generation may call a model; checking never does.
"""

from .diff import check, type_accepts
from .generate import (
    DEFAULT_RANGE_POLICY,
    RangePolicy,
    generate_column_contract,
    generate_contract,
    generate_table_contract,
)
from .models import (
    CONTRACT_SCHEMA_VERSION,
    ColumnContract,
    Contract,
    Defaults,
    Severity,
    TableContract,
)
from .schema import ContractError, dump, dumps, load, loads
from .violations import DEFAULT_SEVERITY, Report, Violation, ViolationType

__all__ = [
    "CONTRACT_SCHEMA_VERSION",
    "DEFAULT_RANGE_POLICY",
    "DEFAULT_SEVERITY",
    "ColumnContract",
    "Contract",
    "ContractError",
    "Defaults",
    "RangePolicy",
    "Report",
    "Severity",
    "TableContract",
    "Violation",
    "ViolationType",
    "check",
    "dump",
    "dumps",
    "generate_column_contract",
    "generate_contract",
    "generate_table_contract",
    "load",
    "loads",
    "type_accepts",
]
