"""Builds the list of real datasets the false-positive run measures against.

The manifest is committed and the data is not. Redistributing other people's
datasets inside this repository would mean auditing every licence and carrying
tens of megabytes forever; a list of URLs is reviewable in a pull request and
costs nothing. The trade is that a run depends on those URLs still resolving,
which is why the runner reports fetch failures loudly rather than quietly
measuring a smaller sample than it claims.

Sources are deliberately boring and well-known: vega-datasets and
plotly/datasets are both widely mirrored, stable for years, and permissively
licensed. Neither was chosen to flatter the tool — they are simply the largest
collections of real CSVs that can be enumerated programmatically rather than
hand-copied, which matters because a manifest typed from memory is a manifest
full of URLs that never existed.

Regenerate with:
    python tests/corpus/build_manifest.py
"""

from __future__ import annotations

import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST = os.path.join(HERE, "manifest.json")

# Small enough that a full run is minutes rather than an afternoon; large enough
# that a profile means something. Under a kilobyte is usually a stub or a lookup
# table with three rows, which would pad the sample without testing anything.
MIN_BYTES = 2_000
MAX_BYTES = 2_000_000

SOURCES = (
    {
        "repo": "vega/vega-datasets",
        "path": "data",
        "licence": "BSD-3-Clause",
    },
    {
        "repo": "plotly/datasets",
        "path": "",
        "licence": "MIT",
    },
)

TARGET = 90


def _listing(repo: str, path: str) -> list[dict]:
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.load(response)
    if isinstance(payload, dict):
        raise RuntimeError(f"{repo}: {payload.get('message')}")
    return payload


def build() -> list[dict]:
    entries: list[dict] = []
    for source in SOURCES:
        for item in _listing(source["repo"], source["path"]):
            if not item["name"].endswith(".csv"):
                continue
            if not MIN_BYTES <= item["size"] <= MAX_BYTES:
                continue
            entries.append(
                {
                    "name": f"{source['repo'].split('/')[-1]}/{item['name'][:-4]}",
                    "url": item["download_url"],
                    "bytes": item["size"],
                    "source": source["repo"],
                    "licence": source["licence"],
                }
            )

    # Sorted by name rather than by size or source, so the sample is not
    # accidentally weighted toward one repository's idea of a normal dataset,
    # and so regenerating produces a reviewable diff instead of a reshuffle.
    entries.sort(key=lambda e: e["name"])

    # Take an even spread across the sorted list rather than the first N, which
    # would be alphabetical and therefore mostly one source.
    if len(entries) > TARGET:
        step = len(entries) / TARGET
        entries = [entries[int(i * step)] for i in range(TARGET)]
    return entries


if __name__ == "__main__":
    datasets = build()
    with open(MANIFEST, "w", encoding="utf-8") as handle:
        json.dump({"datasets": datasets}, handle, indent=2)
        handle.write("\n")
    total = sum(d["bytes"] for d in datasets)
    print(f"{len(datasets)} datasets, {total / 1_000_000:.1f} MB total")
    for source in SOURCES:
        count = sum(1 for d in datasets if d["source"] == source["repo"])
        print(f"  {source['repo']:<24} {count}")
