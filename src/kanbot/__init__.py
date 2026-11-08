"""Kanbot, a local-only kanban board: JSON API for agents, web UI for humans. Stdlib only."""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml is the single source of the version; this reads it from the install.
try:
    __version__ = version("kanbot")
except PackageNotFoundError:  # Running from a source tree that isn't installed.
    __version__ = "0.0.0"
