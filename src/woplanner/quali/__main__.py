"""Allow ``python -m woplanner.quali`` to launch the monitor."""

from .gui import main


if __name__ == "__main__":
    raise SystemExit(main())
