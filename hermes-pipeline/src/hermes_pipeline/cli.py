"""CLI entry point for pipeline-watch."""

import argparse


def main():
    parser = argparse.ArgumentParser(description="Hermes pipeline orchestrator")
    parser.add_argument("--version", action="version", version="0.1.0")
    parser.parse_args()


if __name__ == "__main__":
    main()
