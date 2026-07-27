from __future__ import annotations

import os

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture_path(name: str) -> str:
    path = os.path.join(FIXTURE_DIR, name)
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Missing fixture {name}. Regenerate with: python tests/fixtures/generate.py"
        )
    return path
