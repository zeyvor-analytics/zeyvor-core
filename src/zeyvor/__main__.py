"""``python -m zeyvor`` — identical to the ``zeyvor`` command.

Useful when the console script is not on PATH, and a single code path means the
two can never drift apart. The profile summary this module used to print by hand
is now ``zeyvor profile``.
"""

from __future__ import annotations

import sys

from .cli.main import main

if __name__ == "__main__":
    sys.exit(main())
