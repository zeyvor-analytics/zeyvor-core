"""Terminal rendering, including the paths that only occur on other machines.

The ASCII fallback exists for a legacy Windows console code page and had never
executed anywhere before these tests: on a UTF-8 terminal the Unicode branch is
always taken, so a crash there would only ever have surfaced on a user's
machine.
"""

from __future__ import annotations

import io

from zeyvor.cli.render import ASCII_SYMBOLS, UNICODE_SYMBOLS, Console


class FakeStream(io.StringIO):
    """A stream with a chosen encoding, standing in for a console."""

    def __init__(self, encoding: str = "utf-8", tty: bool = False) -> None:
        super().__init__()
        self._encoding = encoding
        self._tty = tty

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    def isatty(self) -> bool:
        return self._tty


# ── symbols ───────────────────────────────────────────────────────────────────


def test_utf8_console_gets_the_unicode_symbols():
    console = Console(stdout=FakeStream("utf-8"))
    assert console.symbols is UNICODE_SYMBOLS


def test_legacy_windows_code_page_falls_back_to_ascii():
    """cp1252 cannot encode a checkmark, and an exception here would turn a
    passing check into a stack trace."""
    console = Console(stdout=FakeStream("cp1252"))
    assert console.symbols is ASCII_SYMBOLS


def test_the_fallback_actually_encodes_on_that_code_page():
    stream = FakeStream("cp1252")
    console = Console(stdout=stream)
    console.success("all good")
    console.failure("broken")
    console.warning("hmm")
    # The real failure mode is an encoding error at write time, so prove the
    # bytes survive the round trip rather than merely checking which set was
    # chosen.
    stream.getvalue().encode("cp1252")


def test_an_encoding_free_stream_falls_back():
    class NoEncoding(io.StringIO):
        encoding = None

    assert Console(stdout=NoEncoding()).symbols is ASCII_SYMBOLS


def test_an_unknown_encoding_name_does_not_crash():
    assert Console(stdout=FakeStream("not-a-real-codec")).symbols is ASCII_SYMBOLS


# ── colour ────────────────────────────────────────────────────────────────────


def test_colour_is_off_when_not_a_terminal():
    console = Console(stdout=FakeStream("utf-8", tty=False))
    assert console.colour is False
    assert "\033[" not in console.tint("x", "red")


def test_colour_is_on_for_a_terminal(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert Console(stdout=FakeStream("utf-8", tty=True)).colour is True


def test_no_color_is_respected(monkeypatch):
    """https://no-color.org — an env var users expect to be honoured."""
    monkeypatch.setenv("NO_COLOR", "1")
    assert Console(stdout=FakeStream("utf-8", tty=True)).colour is False


def test_dumb_terminals_get_no_escape_codes(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "dumb")
    assert Console(stdout=FakeStream("utf-8", tty=True)).colour is False


def test_an_unknown_colour_name_is_ignored_rather_than_raising():
    console = Console(stdout=FakeStream("utf-8"), colour=True)
    assert console.tint("x", "chartreuse") == "x"


# ── streams ───────────────────────────────────────────────────────────────────


def test_narration_and_answers_go_to_different_streams():
    out, err = FakeStream(), FakeStream()
    console = Console(stdout=out, stderr=err)

    console.out("the answer")
    console.step("narration")
    console.error("a problem")

    assert "the answer" in out.getvalue()
    assert "narration" not in out.getvalue()
    assert "narration" in err.getvalue()
    assert "a problem" in err.getvalue()


def test_quiet_silences_narration_only():
    out, err = FakeStream(), FakeStream()
    console = Console(stdout=out, stderr=err, quiet=True)

    console.step("narration")
    console.success("the answer")

    assert err.getvalue() == ""
    assert "the answer" in out.getvalue()


def test_wrapping_never_exceeds_the_width():
    console = Console(stdout=FakeStream())
    wrapped = console.wrap("word " * 200, indent="    ")
    assert all(len(line) <= 100 for line in wrapped.splitlines())
    assert all(line.startswith("    ") for line in wrapped.splitlines())
