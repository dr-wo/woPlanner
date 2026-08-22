"""Allow ``python -m woplanner.standard`` to launch the standard planner."""

from .gui import main


if __name__ == "__main__":
    raise SystemExit(main())
