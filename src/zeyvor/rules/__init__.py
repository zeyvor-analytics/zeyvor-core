"""Rules that compare one column against another, within a row.

Everything else in Zeyvor looks at a column on its own, or at two tables
through a join. Neither can see a row where `shipped_at` precedes `ordered_at`:
both values are real timestamps, neither is null, and each sits inside its own
declared range. The row is the right shape and the wrong meaning, and the
meaning lives in the relationship between the columns.

`grammar` parses and compiles the expression, `measure` runs it, and `check`
turns counts into findings — the same three-part split the relations package
uses, for the same reason: parsing must be testable without a database.
"""

from .check import check_rules
from .grammar import RuleError, compile_rule, parse_rule, referenced_columns
from .measure import RuleMeasurement, measure_rules

__all__ = [
    "RuleError",
    "RuleMeasurement",
    "check_rules",
    "compile_rule",
    "measure_rules",
    "parse_rule",
    "referenced_columns",
]
