"""Regenerate the CSV fixtures.

Run with: python tests/fixtures/generate.py

Each fixture encodes a real failure mode rather than generic dummy data, so the
suite reads as a specification of what Zeyvor is for.
"""

from __future__ import annotations

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))

STATUSES = ["pending", "shipped", "delivered", "refunded"]
COUNTRIES = ["US", "GB", "DE", "FR"]


def write(name: str, header: list[str], rows: list[list[object]]) -> None:
    path = os.path.join(HERE, name)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    print(f"wrote {name} ({len(rows)} rows)")


def clean_orders() -> None:
    """The healthy baseline every other fixture is a deviation from."""
    rows = []
    for i in range(100):
        rows.append(
            [
                1000 + i,
                f"user{i}@example.com",
                f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}",
                STATUSES[i % len(STATUSES)],
                round(19.99 + i * 3.5, 2),
                COUNTRIES[i % len(COUNTRIES)],
                i % 7,
            ]
        )
    write(
        "clean_orders.csv",
        ["order_id", "customer_email", "signup_date", "status", "amount", "country", "item_count"],
        rows,
    )


def broken_dates() -> None:
    """The flagship failure: a date column quietly gaining Unix timestamps.

    97 ISO dates, 3 epoch seconds. Nothing is null, nothing is duplicated, the
    row count is normal — every conventional check passes.
    """
    rows = []
    for i in range(97):
        rows.append([2000 + i, f"2024-{(i % 12) + 1:02d}-{(i % 28) + 1:02d}", "shipped"])
    for i, epoch in enumerate([1714089600, 1714176000, 1714262400]):
        rows.append([2097 + i, epoch, "shipped"])
    write("broken_dates.csv", ["order_id", "signup_date", "status"], rows)


def excel_serials() -> None:
    """A date column that has become Excel serial numbers outright."""
    rows = [[3000 + i, 45231 + i] for i in range(50)]
    write("excel_serials.csv", ["invoice_id", "issued_date"], rows)


def messy() -> None:
    """Many hygiene problems at once, including PII inside a free-text column."""
    rows = [
        [
            "00123",
            " Alice Smith ",
            "$1,299.00",
            "12.5%",
            "N/A",
            "contact me at alice@corp.com",
            "ACTIVE",
        ],
        ["00124", "bob jones", "$2,450.50", "8%", "null", "no email here", "active"],
        [
            "00125",
            "CAROL WHITE ",
            "$980.00",
            "15.2%",
            "2024-03-11",
            "reach dave@corp.com or call",
            "Active",
        ],
        [
            "00126",
            " dave brown",
            "$1,100.25",
            "9.9%",
            "11.03.2024",
            "Ping erin@corp.com pls",
            "ACTIVE",
        ],
        ["00127", "Erin Black", "$3,200.00", "22%", "3/11/2024", "cafÃ© visit notes", "inactive"],
        ["00128", "frank green", "$750.75", "5%", "-", "nothing", "INACTIVE"],
        [
            "00129",
            "Grace Hall ",
            "$1,875.00",
            "18.4%",
            "2024-04-01",
            "email: grace@corp.com",
            "Inactive",
        ],
        ["00130", "HENRY KING", "$2,050.00", "11%", "none", "call 555-123-4567", "active"],
    ]
    write(
        "messy.csv",
        ["account_code", "full_name", "revenue", "growth_rate", "signup_date", "notes", "status"],
        rows,
    )


def enum_drift() -> None:
    """The same status column after an upstream release added a category."""
    rows = []
    extended = STATUSES + ["awaiting_pickup"]
    for i in range(60):
        rows.append([4000 + i, extended[i % len(extended)]])
    write("enum_drift.csv", ["order_id", "status"], rows)


def edge_cases() -> None:
    """Degenerate shapes that must not crash or produce nonsense."""
    rows = [
        [1, "", "same", None, "0001"],
        [2, "", "same", None, "0002"],
        [3, "", "same", None, "0003"],
    ]
    write(
        "edge_cases.csv",
        ["id", "always_blank", "constant", "always_null", "padded_id"],
        rows,
    )


def wide() -> None:
    """Enough columns to exercise query batching."""
    header = [f"col_{i:03d}" for i in range(60)]
    rows = [[f"{i}-{j}" for j in range(60)] for i in range(20)]
    write("wide.csv", header, rows)


def us_dates() -> None:
    """Dates in a format no SQL engine will cast.

    DuckDB parses ISO dates and refuses '3/11/2024'. Without format evidence
    this column would be written off as free text, and no date contract could
    ever be generated for it.
    """
    rows = [[6000 + i, f"{(i % 12) + 1}/{(i % 28) + 1}/2024"] for i in range(50)]
    write("us_dates.csv", ["ticket_id", "closed_date"], rows)


def unit_shift() -> None:
    """Currency that switched from dollars to cents partway through.

    Types never change and nothing is null; only the magnitude moves. This is
    what a range clause in a contract exists to catch.
    """
    rows = [[5000 + i, round(20.0 + i, 2)] for i in range(80)]
    rows += [[5080 + i, (20.0 + i) * 100] for i in range(20)]
    write("unit_shift.csv", ["payment_id", "amount"], rows)


if __name__ == "__main__":
    clean_orders()
    broken_dates()
    excel_serials()
    messy()
    enum_drift()
    edge_cases()
    wide()
    us_dates()
    unit_shift()
