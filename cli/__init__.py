"""AEGISAI command-line entry point."""

__all__ = ["main"]


def main(argv=None) -> int:
    from .main import main as cli_main

    return cli_main(argv)
