"""Querly Studio backend.

A local FastAPI application that exposes the Querly compiler, runtime, adapter
and conversion engines to the Studio IDE.  This package is deliberately an
integration layer: the language core and database adapters remain independent
of the GUI.
"""

from __future__ import annotations

__version__ = "0.10.0"

APP_NAME = "Querly Studio"
