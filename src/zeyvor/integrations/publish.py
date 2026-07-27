"""Rendering a report for somewhere other than the author's terminal.

A pull-request comment and a Slack message are *publications*. The terminal is
not: it is one person looking at their own data on their own machine.

That distinction drives the one rule here. A check report can legitimately name
category values — status names, plan tiers, region codes — because knowing which
value is new is what makes a finding actionable. In a terminal that is exactly
right. Posted to a comment on a public repository, or into a Slack channel with
two hundred people in it, the same text is a leak.

So published output **redacts values by default** and reports types, counts,
shares and shapes instead. Teams on private repositories can opt back in. The
CLI's own output is unchanged; it is the act of publishing that tightens.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

MARKER = "<!-- zeyvor-report -->"
"""Hidden marker used to find and update a previous comment, so a week of
pushes leaves one comment rather than forty."""

# Fields that can carry raw values from the data.
VALUE_BEARING = ("new_categories", "missing_categories", "offending")


def redact_violation(violation: dict[str, Any]) -> dict[str, Any]:
    """Strip values from a violation, keeping every number and shape."""
    out = dict(violation)
    evidence = dict(out.get("evidence") or {})

    for key in ("new_categories", "missing_categories"):
        values = evidence.get(key)
        if isinstance(values, list):
            evidence[key] = f"{len(values)} value(s) — run `zeyvor check` locally to see them"
    if evidence:
        out["evidence"] = evidence

    # `found` is prose that frequently quotes values, e.g.
    # "1 new value: 'awaiting_pickup' (12 rows)".
    if violation.get("type") in {"new_category", "category_disappeared"}:
        count = _count_in(violation.get("evidence") or {})
        out["found"] = (
            f"{count} value(s) not in the contract"
            if violation.get("type") == "new_category"
            else f"{count} contracted value(s) missing"
        )
    return out


def _count_in(evidence: dict[str, Any]) -> int:
    for key in ("new_categories", "missing_categories"):
        value = evidence.get(key)
        if isinstance(value, list):
            return len(value)
    return 0


def _prepare(report: dict[str, Any], *, show_values: bool) -> dict[str, Any]:
    if show_values:
        return report
    out = dict(report)
    out["violations"] = [redact_violation(v) for v in report.get("violations", [])]
    return out


# ── markdown ──────────────────────────────────────────────────────────────────


def to_markdown(
    report: dict[str, Any],
    *,
    show_values: bool = False,
    title: str = "Zeyvor data contract check",
) -> str:
    """Render a report dict as GitHub-flavoured markdown."""
    report = _prepare(report, show_values=show_values)
    violations = report.get("violations", [])
    failed = report.get("failed", 0)
    warned = report.get("warned", 0)

    lines = [MARKER, f"## {title}", ""]

    if not violations:
        lines += [
            f"✅ **{report.get('columns_checked', 0)} columns** across "
            f"**{report.get('tables_checked', 0)} table(s)** match the contract.",
        ]
        return "\n".join(lines) + "\n"

    verdict = "❌ **Failed**" if failed else "⚠️ **Warnings only**"
    lines += [
        f"{verdict} — {failed} failed, {warned} warned across "
        f"{report.get('columns_checked', 0)} columns.",
        "",
        "| | Column | Finding | Detail |",
        "|---|---|---|---|",
    ]
    for violation in violations:
        icon = "❌" if violation.get("severity") == "fail" else "⚠️"
        target = violation.get("column") or violation.get("table", "")
        found = _clean_cell(violation.get("found", ""))
        lines.append(f"| {icon} | `{target}` | `{violation.get('type', '')}` | {found} |")

    lines += ["", "<details><summary>What to do</summary>", ""]
    for violation in violations:
        target = violation.get("column") or violation.get("table", "")
        lines.append(f"**`{target}` — {violation.get('type', '')}**")
        for label, key in (("Contract", "expected"), ("Found", "found")):
            if violation.get(key):
                lines.append(f"- {label}: {_clean_cell(violation[key])}")
        if violation.get("detail"):
            lines.append(f"- {_clean_cell(violation['detail'])}")
        if violation.get("remedy"):
            lines.append(f"- → {_clean_cell(violation['remedy'])}")
        lines.append("")
    lines.append("</details>")

    if not show_values:
        lines += [
            "",
            "<sub>Values are omitted from this comment. Run `zeyvor check` "
            "locally for the full detail.</sub>",
        ]
    return "\n".join(lines) + "\n"


def _clean_cell(text: str) -> str:
    """Keep a table cell on one line and out of markdown's way."""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


# ── slack ─────────────────────────────────────────────────────────────────────


def to_slack_blocks(
    report: dict[str, Any],
    *,
    show_values: bool = False,
    context: str = "",
) -> dict[str, Any]:
    """Build a Slack message payload. Pure, so it can be tested without a network."""
    report = _prepare(report, show_values=show_values)
    violations = report.get("violations", [])
    failed = report.get("failed", 0)
    warned = report.get("warned", 0)

    if not violations:
        headline = f"✅ Zeyvor: {report.get('columns_checked', 0)} columns match the contract"
    else:
        headline = f"{'❌' if failed else '⚠️'} Zeyvor: {failed} failed, {warned} warned"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": headline[:150]}}
    ]
    if context:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": context}]})

    # Slack truncates aggressively, so lead with failures and cap the list.
    ranked = sorted(violations, key=lambda v: 0 if v.get("severity") == "fail" else 1)
    for violation in ranked[:10]:
        icon = "❌" if violation.get("severity") == "fail" else "⚠️"
        target = violation.get("column") or violation.get("table", "")
        body = f"{icon} *{target}* — `{violation.get('type', '')}`"
        if violation.get("found"):
            body += f"\n{_clean_cell(violation['found'])}"
        if violation.get("remedy"):
            body += f"\n_{_clean_cell(violation['remedy'])}_"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": body[:2900]}})

    if len(ranked) > 10:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"…and {len(ranked) - 10} more findings"}],
            }
        )
    return {"text": headline, "blocks": blocks}


def post_to_slack(webhook_url: str, payload: dict[str, Any], *, timeout: float = 10.0) -> None:
    """POST a payload to a Slack webhook using the standard library only.

    Adding an HTTP client as a dependency for one request would be a poor trade
    for a tool whose whole install is meant to be two packages.
    """
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - the URL is user-supplied by design
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            if response.status >= 300:
                raise RuntimeError(f"Slack returned {response.status}")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Slack: {exc.reason}") from None


__all__ = [
    "MARKER",
    "post_to_slack",
    "redact_violation",
    "to_markdown",
    "to_slack_blocks",
]
