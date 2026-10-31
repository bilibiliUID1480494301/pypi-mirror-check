from __future__ import annotations

from . import main


def main_cli() -> None:
    """console_scripts 入口。"""
    raise SystemExit(main())


if __name__ == "__main__":
    main_cli()
