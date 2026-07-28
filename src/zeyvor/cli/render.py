"""Terminal output.

Small but worth care: this is the entire product as far as a first-time user is
concerned. Three rules.

**Colour only for humans.** Escape codes are emitted when stdout is a terminal
and NO_COLOR is unset, so piping into a file or a CI log stays readable.

**stdout carries the answer, stderr carries the narration.** Progress lines and
warnings go to stderr, so `zeyvor check --json | jq` works without filtering.

**Never crash on a symbol.** A checkmark on a Windows console with a legacy code
page raises UnicodeEncodeError, which would turn a passing check into a stack
trace. Where the encoding cannot carry them, ASCII is used instead.
"""

from __future__ import annotations

import os
import shutil
import sys
import textwrap
from typing import TextIO

UNICODE_SYMBOLS = {"ok": "✔", "fail": "✖", "warn": "!", "arrow": "→", "bullet": "·"}
ASCII_SYMBOLS = {"ok": "OK", "fail": "X", "warn": "!", "arrow": "->", "bullet": "-"}

RESET = "\033[0m"
COLOURS = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "dim": "\033[2m",
    "bold": "\033[1m",
}


def _supports_unicode(stream: TextIO) -> bool:
    encoding = getattr(stream, "encoding", None) or ""
    if not encoding:
        return False
    try:
        "✔→".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


class Console:
    def __init__(
        self,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        *,
        colour: bool | None = None,
        quiet: bool = False,
    ) -> None:
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.quiet = quiet
        if colour is None:
            colour = (
                hasattr(self.stdout, "isatty")
                and self.stdout.isatty()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
            )
        self.colour = bool(colour)
        self.symbols = UNICODE_SYMBOLS if _supports_unicode(self.stdout) else ASCII_SYMBOLS

    # ── width ─────────────────────────────────────────────────────────────────

    @property
    def width(self) -> int:
        try:
            return max(shutil.get_terminal_size((88, 24)).columns, 40)
        except Exception:  # pragma: no cover - defensive
            return 88

    def wrap(self, text: str, *, indent: str = "") -> str:
        return textwrap.fill(
            text,
            width=min(self.width, 100),
            initial_indent=indent,
            subsequent_indent=indent,
        )

    # ── writing ───────────────────────────────────────────────────────────────

    def tint(self, text: str, colour: str) -> str:
        if not self.colour or colour not in COLOURS:
            return text
        return f"{COLOURS[colour]}{text}{RESET}"

    def out(self, text: str = "") -> None:
        print(text, file=self.stdout)

    def info(self, text: str = "") -> None:
        """Narration. Goes to stderr so it never pollutes piped output."""
        if not self.quiet:
            print(text, file=self.stderr)

    def step(self, text: str) -> None:
        self.info(self.tint(f"{self.symbols['bullet']} {text}", "dim"))

    def success(self, text: str) -> None:
        self.out(self.tint(f"{self.symbols['ok']} {text}", "green"))

    def failure(self, text: str) -> None:
        self.out(self.tint(f"{self.symbols['fail']} {text}", "red"))

    def warning(self, text: str) -> None:
        self.out(self.tint(f"{self.symbols['warn']} {text}", "yellow"))

    def error(self, text: str) -> None:
        """A problem with the invocation, not with the data."""
        print(self.tint(f"error: {text}", "red"), file=self.stderr)

    def hint(self, text: str) -> None:
        print(self.tint(f"       {text}", "dim"), file=self.stderr)


def render_report(report, console: Console) -> str:
    """Format a check report for a terminal.

    Violations are grouped by column, because several findings on one column are
    one story and reading them interleaved with another column's is confusing.
    """
    symbols = console.symbols
    if not report.violations:
        plural = "s" if report.tables_checked != 1 else ""
        joins = getattr(report, "relationships_checked", 0)
        # Worth saying out loud: somebody who added relationships wants to know
        # they were actually measured, not assume it from silence.
        also = f", and {joins} join{'s' if joins != 1 else ''} are intact" if joins else ""
        return console.tint(
            f"{symbols['ok']} {report.columns_checked} columns across "
            f"{report.tables_checked} table{plural} match the contract{also}.",
            "green",
        )

    ordered: list[str] = []
    for violation in report.violations:
        if violation.target not in ordered:
            ordered.append(violation.target)

    blocks: list[str] = []
    for target in ordered:
        for violation in report.violations:
            if violation.target != target:
                continue
            is_fail = violation.severity.value == "fail"
            mark = symbols["fail"] if is_fail else symbols["warn"]
            head = console.tint(
                f"{mark} {violation.target} — {violation.type.value}",
                "red" if is_fail else "yellow",
            )
            lines = [head]
            if violation.expected:
                lines.append(console.wrap(f"Contract: {violation.expected}", indent="    "))
            if violation.found:
                lines.append(console.wrap(f"Found:    {violation.found}", indent="    "))
            if violation.detail:
                lines.append(console.wrap(violation.detail, indent="    "))
            if violation.remedy:
                lines.append(console.wrap(f"{symbols['arrow']} {violation.remedy}", indent="    "))
            blocks.append("\n".join(lines))

    failed, warned = len(report.failures), len(report.warnings)
    scope = f"{report.columns_checked} columns"
    joins = getattr(report, "relationships_checked", 0)
    if joins:
        scope += f" and {joins} relationship{'s' if joins != 1 else ''}"
    summary = f"{failed} failed, {warned} warned across {scope}"
    return "\n\n".join(blocks) + "\n\n" + console.tint(summary, "red" if failed else "yellow")


__all__ = ["Console", "render_report", "UNICODE_SYMBOLS", "ASCII_SYMBOLS"]
