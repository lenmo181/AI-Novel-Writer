"""Lazy access to the flattened cmcloud core modules."""

import importlib


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)
    module = importlib.import_module("core_" + name)
    globals()[name] = module
    return module
