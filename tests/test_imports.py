"""Smoke tests for the public API surface.

These tests verify that every name listed in the package's ``__all__`` is
importable from the top-level package, and that the console-script entry
points resolve to a callable ``main``. They do not exercise behavior —
that's the existing fleet-tools test suite, which we'll port in
follow-up PRs.
"""

from __future__ import annotations

import importlib

import pytest


def test_package_imports() -> None:
    """The package itself imports cleanly."""
    pkg = importlib.import_module("musubi_harness")
    assert pkg.__doc__ is not None


def test_all_names_importable() -> None:
    """Every name in musubi_harness.__all__ is importable from the package."""
    pkg = importlib.import_module("musubi_harness")
    missing = [name for name in pkg.__all__ if not hasattr(pkg, name)]
    assert not missing, f"missing public names: {missing}"


@pytest.mark.parametrize(
    "module_path,callable_name",
    [
        ("musubi_harness.cli.harness", "main"),
        ("musubi_harness.cli.conformance", "main"),
    ],
)
def test_cli_entry_points(module_path: str, callable_name: str) -> None:
    """Each console script's entry-point function exists and is callable."""
    module = importlib.import_module(module_path)
    fn = getattr(module, callable_name)
    assert callable(fn)
