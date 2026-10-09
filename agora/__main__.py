"""Allow running the package as ``python -m agora``."""

import sys

from agora.cli import main

if __name__ == "__main__":
    sys.exit(main())
