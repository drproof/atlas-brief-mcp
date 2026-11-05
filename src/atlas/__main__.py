import sys

from .cli import main

if __name__ == "__main__":
    # Ohne sys.exit() landen Fehler-Exit-Codes (1) als 0 im Shell-Status.
    sys.exit(main())
