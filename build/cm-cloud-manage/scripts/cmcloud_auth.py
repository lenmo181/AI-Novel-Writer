"""Lazy access to the flattened cmcloud authentication modules."""

import importlib


def __getattr__(name):
    if name.startswith("_"):
        raise AttributeError(name)
    module = importlib.import_module("auth_" + name)
    globals()[name] = module
    return module
