import sys

from .cli import main

if __name__ == "__main__":
    # Without sys.exit(), non-zero exit codes (1) would become 0 in the shell.
    sys.exit(main())
