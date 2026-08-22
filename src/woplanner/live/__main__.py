"""Allow ``python -m woplanner.live`` to launch the live planner."""

from .gui import main


if __name__ == "__main__":
    raise SystemExit(main())
