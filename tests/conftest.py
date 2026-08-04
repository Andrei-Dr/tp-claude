"""Load the hyphenated, extensionless `tp-claude` script as an importable module."""
import importlib.machinery
import importlib.util
import pathlib
import sys

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "tp-claude"


def _load():
    spec = importlib.util.spec_from_loader(
        "tpclaude", importlib.machinery.SourceFileLoader("tpclaude", str(SCRIPT))
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["tpclaude"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def tpc():
    return _load()


@pytest.fixture(scope="session")
def script_path():
    return SCRIPT
