"""A small expression language, and the SQL it compiles to.

The contract file is something a human reads and approves in a pull request.
That is the whole reason it is YAML describing shapes rather than a script, and
it is why this is a grammar Zeyvor understands rather than a SQL fragment
pasted into a query. A raw fragment would be more powerful and take an
afternoon; it would also make every contract an executable artifact running
against a warehouse, and make a contract written for Postgres fail on BigQuery.

So the vocabulary is deliberately small:

    shipped_at >= ordered_at
    discount <= subtotal
    abs(total - (subtotal - discount)) <= 0.01
    status = 'shipped' implies shipped_at is not null

Comparisons, arithmetic, `and`/`or`/`not`, `is null`, `implies`, and two
functions. No subqueries, no window functions, nothing that reaches outside the
row. Those belong in a transformation, not in a description of what a row means.

**Null is not a violation.** A rule over a null column evaluates to NULL, and a
NULL rule is counted as "could not tell", never as broken. This follows SQL's
own three-valued logic rather than fighting it, and it keeps the checks from
overlapping: whether a column is allowed to be null is what `nullable` says,
and a rule restating it would report the same problem twice. To assert
something *about* nulls, say so — `is not null`, or the `implies` on the right
of a condition.

Every column reference is cast according to the type the contract declares for
it, because profiling reads a source as text and `'9' > '10'` is true in text
and false in every arithmetic anyone means. The cast is a *try* cast: a value
that cannot be read as its declared type becomes NULL, and so is not counted as
breaking the rule. That is deliberate. A column full of the wrong type is
already `type_contaminated`, and reporting it a second time through every rule
that happens to touch it would bury the cause under its consequences.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class RuleError(ValueError):
    """A rule that could not be parsed, or that names something unknown."""


# ── the tree ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Column:
    name: str


@dataclass(frozen=True)
class Literal:
    value: object
    """A number, a string, True/False, or None for the null literal."""


@dataclass(frozen=True)
class Unary:
    op: str
    operand: object


@dataclass(frozen=True)
class Binary:
    op: str
    left: object
    right: object


@dataclass(frozen=True)
class IsNull:
    operand: object
    negated: bool


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple


Node = object


# ── tokens ────────────────────────────────────────────────────────────────────

KEYWORDS = frozenset({"and", "or", "not", "is", "null", "implies", "true", "false"})

# Longest first: `<=` must be tried before `<`, or `a <= b` tokenises as `<`
# followed by a stray `=` and the error message blames the wrong character.
_OPERATORS = ("<=", ">=", "!=", "<>", "==", "=", "<", ">", "+", "-", "*", "/")

FUNCTIONS = frozenset({"abs", "length"})

_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<number>\d+\.\d+|\.\d+|\d+)
  | (?P<string>'(?:[^']|'')*')
  | (?P<quoted>"(?:[^"]|"")*")
  | (?P<name>[A-Za-z_][A-Za-z0-9_]*)
  | (?P<op><=|>=|!=|<>|==|=|<|>|\+|-|\*|/)
  | (?P<paren>[()])
  | (?P<comma>,)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    at: int

    @property
    def word(self) -> str:
        return self.text.lower()


def _tokenise(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    position = 0
    while position < len(source):
        match = _TOKEN.match(source, position)
        if match is None:
            raise RuleError(
                f"cannot read {source[position]!r} at position {position} in {source!r}"
            )
        kind = match.lastgroup or ""
        text = match.group()
        position = match.end()
        if kind == "space":
            continue
        if kind == "name" and text.lower() in KEYWORDS:
            kind = "keyword"
        tokens.append(_Token(kind, text, match.start()))
    return tokens


# ── the parser ────────────────────────────────────────────────────────────────

_COMPARISONS = {"=", "==", "!=", "<>", "<", "<=", ">", ">="}


class _Parser:
    def __init__(self, source: str) -> None:
        self.source = source
        self.tokens = _tokenise(source)
        self.index = 0

    # -- token plumbing --

    def _peek(self) -> _Token | None:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> _Token:
        token = self._peek()
        if token is None:
            raise RuleError(f"{self.source!r} ends before it is finished")
        self.index += 1
        return token

    def _at_keyword(self, word: str) -> bool:
        token = self._peek()
        return token is not None and token.kind == "keyword" and token.word == word

    def _eat_keyword(self, word: str) -> bool:
        if self._at_keyword(word):
            self.index += 1
            return True
        return False

    def _expect_keyword(self, word: str) -> None:
        if not self._eat_keyword(word):
            found = self._peek()
            shown = repr(found.text) if found else "the end of the rule"
            raise RuleError(f"expected {word!r} but found {shown} in {self.source!r}")

    def _at_op(self, *ops: str) -> str | None:
        token = self._peek()
        if token is not None and token.kind == "op" and token.text in ops:
            return token.text
        return None

    # -- the grammar, loosest binding first --

    def parse(self) -> Node:
        node = self._implies()
        leftover = self._peek()
        if leftover is not None:
            raise RuleError(
                f"unexpected {leftover.text!r} at position {leftover.at} in {self.source!r}"
            )
        return node

    def _implies(self) -> Node:
        left = self._or()
        # Right-associative, so `a implies b implies c` reads as
        # `a implies (b implies c)` — the same way people say it aloud.
        if self._eat_keyword("implies"):
            return Binary("implies", left, self._implies())
        return left

    def _or(self) -> Node:
        node = self._and()
        while self._eat_keyword("or"):
            node = Binary("or", node, self._and())
        return node

    def _and(self) -> Node:
        node = self._not()
        while self._eat_keyword("and"):
            node = Binary("and", node, self._not())
        return node

    def _not(self) -> Node:
        if self._eat_keyword("not"):
            return Unary("not", self._not())
        return self._comparison()

    def _comparison(self) -> Node:
        left = self._additive()
        if self._eat_keyword("is"):
            negated = self._eat_keyword("not")
            self._expect_keyword("null")
            return IsNull(left, negated)
        op = self._at_op(*_COMPARISONS)
        if op is not None:
            self.index += 1
            return Binary(op, left, self._additive())
        return left

    def _additive(self) -> Node:
        node = self._multiplicative()
        while (op := self._at_op("+", "-")) is not None:
            self.index += 1
            node = Binary(op, node, self._multiplicative())
        return node

    def _multiplicative(self) -> Node:
        node = self._unary()
        while (op := self._at_op("*", "/")) is not None:
            self.index += 1
            node = Binary(op, node, self._unary())
        return node

    def _unary(self) -> Node:
        if self._at_op("-") is not None:
            self.index += 1
            return Unary("-", self._unary())
        return self._primary()

    def _primary(self) -> Node:
        token = self._take()

        if token.kind == "paren" and token.text == "(":
            node = self._implies()
            closing = self._peek()
            if closing is None or closing.text != ")":
                raise RuleError(f"unclosed ( in {self.source!r}")
            self.index += 1
            return node

        if token.kind == "number":
            text = token.text
            return Literal(float(text) if "." in text else int(text))

        if token.kind == "string":
            return Literal(token.text[1:-1].replace("''", "'"))

        if token.kind == "quoted":
            return Column(token.text[1:-1].replace('""', '"'))

        if token.kind == "keyword":
            if token.word == "true":
                return Literal(True)
            if token.word == "false":
                return Literal(False)
            if token.word == "null":
                return Literal(None)
            raise RuleError(f"{token.text!r} cannot start a value in {self.source!r}")

        if token.kind == "name":
            following = self._peek()
            if following is not None and following.text == "(":
                return self._call(token)
            return Column(token.text)

        raise RuleError(f"unexpected {token.text!r} at position {token.at} in {self.source!r}")

    def _call(self, name_token: _Token) -> Node:
        name = name_token.text.lower()
        if name not in FUNCTIONS:
            known = ", ".join(sorted(FUNCTIONS))
            raise RuleError(
                f"{name_token.text!r} is not a function Zeyvor knows. Available: {known}"
            )
        self.index += 1  # the opening paren
        args: list[Node] = []
        if not (self._peek() is not None and self._peek().text == ")"):  # type: ignore[union-attr]
            args.append(self._implies())
            while self._peek() is not None and self._peek().text == ",":  # type: ignore[union-attr]
                self.index += 1
                args.append(self._implies())
        closing = self._peek()
        if closing is None or closing.text != ")":
            raise RuleError(f"unclosed ( after {name!r} in {self.source!r}")
        self.index += 1
        if len(args) != 1:
            raise RuleError(f"{name}() takes one argument, given {len(args)}")
        return Call(name, tuple(args))


def parse_rule(source: str) -> Node:
    """Parse a rule, or raise `RuleError` saying where it stopped making sense."""
    if not source or not source.strip():
        raise RuleError("a rule cannot be empty")
    return _Parser(source).parse()


def referenced_columns(node: Node) -> set[str]:
    """Every column the rule mentions, for checking against the table."""
    if isinstance(node, Column):
        return {node.name}
    if isinstance(node, Unary):
        return referenced_columns(node.operand)
    if isinstance(node, Binary):
        return referenced_columns(node.left) | referenced_columns(node.right)
    if isinstance(node, IsNull):
        return referenced_columns(node.operand)
    if isinstance(node, Call):
        found: set[str] = set()
        for argument in node.args:
            found |= referenced_columns(argument)
        return found
    return set()


# ── compiling to SQL ──────────────────────────────────────────────────────────

_SQL_COMPARISON = {"=": "=", "==": "=", "!=": "<>", "<>": "<>"}


def _column_sql(name: str, dialect, types: Mapping[str, str | None]) -> str:
    """A column reference, cast to whatever the contract says it holds.

    The source is read as text, so an uncast comparison is a text comparison —
    which quietly gives the wrong answer for numbers and dates rather than
    failing. `try_cast` rather than `cast` so one unparseable value yields NULL
    instead of aborting the whole query.
    """
    quoted = dialect.quote_ident(name)
    family = (types.get(name) or "text").lower()
    if family in ("integer", "float", "number", "numeric", "decimal"):
        return dialect.try_cast(quoted, dialect.float_type)
    if family == "date":
        return dialect.try_cast(quoted, dialect.date_type)
    if family == "timestamp":
        return dialect.try_cast(quoted, dialect.timestamp_type)
    if family == "boolean":
        return dialect.try_cast(quoted, dialect.bool_type)
    return dialect.as_text(quoted)


def compile_rule(node: Node, dialect, types: Mapping[str, str | None]) -> str:
    """Render the tree as a SQL boolean expression for `dialect`."""
    if isinstance(node, Column):
        return _column_sql(node.name, dialect, types)

    if isinstance(node, Literal):
        value = node.value
        if value is None:
            return "NULL"
        if value is True:
            return "TRUE"
        if value is False:
            return "FALSE"
        if isinstance(value, str):
            return dialect.quote_literal(value)
        return repr(value)

    if isinstance(node, IsNull):
        inner = compile_rule(node.operand, dialect, types)
        return f"({inner} IS NOT NULL)" if node.negated else f"({inner} IS NULL)"

    if isinstance(node, Unary):
        inner = compile_rule(node.operand, dialect, types)
        return f"(NOT {inner})" if node.op == "not" else f"(-{inner})"

    if isinstance(node, Call):
        inner = compile_rule(node.args[0], dialect, types)
        if node.name == "length":
            return dialect.length(inner)
        return f"ABS({inner})"

    if isinstance(node, Binary):
        left = compile_rule(node.left, dialect, types)
        right = compile_rule(node.right, dialect, types)
        if node.op == "implies":
            # `a implies b` is `(not a) or b`. Written out rather than given its
            # own SQL construct because no engine has one, and three-valued
            # logic then falls out for free: an unknown premise gives an
            # unknown result, which is counted as "could not tell".
            return f"((NOT {left}) OR {right})"
        if node.op in ("and", "or"):
            return f"({left} {node.op.upper()} {right})"
        if node.op in _SQL_COMPARISON:
            return f"({left} {_SQL_COMPARISON[node.op]} {right})"
        return f"({left} {node.op} {right})"

    raise RuleError(f"cannot compile {node!r}")


def validate_rule(source: str, columns: Sequence[str]) -> Node:
    """Parse, and confirm every column named actually exists on the table."""
    node = parse_rule(source)
    known = {str(name) for name in columns}
    unknown = sorted(referenced_columns(node) - known)
    if unknown:
        detail = ", ".join(repr(name) for name in unknown)
        suggestion = _closest(unknown[0], known)
        hint = f". Did you mean {suggestion!r}?" if suggestion else ""
        raise RuleError(f"rule names a column the table does not have: {detail}{hint}")
    return node


def _closest(name: str, known: set[str]) -> str | None:
    """The nearest known column name, when one is near enough to suggest.

    A typo in a rule is the most likely way to get this wrong, and a bare
    "no such column" leaves someone comparing two lists by eye.
    """
    import difflib

    matches = difflib.get_close_matches(name, sorted(known), n=1, cutoff=0.7)
    return matches[0] if matches else None
