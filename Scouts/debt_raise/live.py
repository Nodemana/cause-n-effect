"""Runnable module for the debt-raise scout. The folder name is hyphenated, so this is the import path."""

import importlib.util
import sys
from pathlib import Path

_IMPL = Path(__file__).resolve().parents[1] / "debt-raise" / "live.py"


def main(argv=None):
    spec = importlib.util.spec_from_file_location("debt_raise_live_impl", _IMPL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    sys.exit(main())
