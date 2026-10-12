"""Runnable module for the insider-buying scout. The folder name is hyphenated, so this is the import path."""

import importlib.util
import sys
from pathlib import Path

_IMPL = Path(__file__).resolve().parents[1] / "insider-buying" / "live.py"


def main(argv=None):
    spec = importlib.util.spec_from_file_location("insider_buying_live_impl", _IMPL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(argv)


if __name__ == "__main__":
    sys.exit(main())
