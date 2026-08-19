"""Command-line entry point."""

import argparse


def main(argv: list[str] | None = None) -> int:
    """Print the command help and return 0."""
    parser = argparse.ArgumentParser(
        prog="workgraph",
        description="Graph workflow orchestrator.",
    )
    parser.parse_args(argv)
    parser.print_help()
    return 0
