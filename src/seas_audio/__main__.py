"""Allow ``python -m seas_audio``."""

from __future__ import annotations

import sys

from seas_audio.cli import main

if __name__ == "__main__":
    sys.exit(main())