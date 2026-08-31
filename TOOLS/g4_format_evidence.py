#!/usr/bin/env python3
"""List real resource extensions before format analysis is admitted."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path

from formats.evidence import build_format_evidence


DEFAULT_EXTENSIONS = (
    ".g4md", ".g4mg", ".g4sk", ".g4pk", ".g4pkm", ".g4tx",
    ".g4mt", ".g4ma", ".g4cm", ".g4la", ".mevbin", ".ptlb", ".cfg.bin",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--extension", action="append", dest="extensions")
    args = parser.parse_args()
    extensions = args.extensions or DEFAULT_EXTENSIONS
    evidence = build_format_evidence(args.roots, extensions)
    print(json.dumps([asdict(item) for item in evidence], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
