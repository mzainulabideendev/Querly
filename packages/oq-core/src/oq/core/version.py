"""Single source of truth for the Querly language-core version."""

from __future__ import annotations

from typing import Final

__all__ = ["VERSION", "VERSION_INFO", "version_string"]


VERSION: Final = "0.1.0.dev0"
"""PEP 440 development version shared by every workspace distribution.

A repository test asserts that each package's ``pyproject.toml`` declares the
same version, so the workspace cannot drift silently.
"""

VERSION_INFO: Final = (0, 1, 0)
"""Numeric ``(major, minor, patch)`` form of :data:`VERSION`."""


def version_string() -> str:
    """Return the version in the form shown by the CLI and SDKs."""
    return VERSION
