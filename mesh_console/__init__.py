# mesh_console/__init__.py
# This file marks the mesh_console directory as a Python package.

# **The version, and the only copy of it the running program can read.**
#
# There is no `[build-system]` here and the project is not installed — the console
# runs from a checkout, over SSH, through `scripts/run_console.py` — so
# `importlib.metadata.version("mesh-console")` raises PackageNotFoundError and
# pyproject's `version` field is unreadable at runtime. That field stayed at the
# 0.1.0 of the initial commit for the whole life of the project for exactly that
# reason: nothing ever read it, so nothing ever forced it to be true. It is now
# kept in step with this by hand, and this is the side that matters.
#
# 1.0.0 rather than a number continuing from 0.1.0, and deliberately nothing like
# 0.10.0: the menu prints this one line above the archive's schema version, and two
# similar-looking numbers next to each other invite the reading that they are the
# same kind of fact. They are not related at all. The collector shows what that
# confusion looks like — its package says 0.5.0 while its schema says 0.10.0.
#
# What forces a bump: this is displayed in the ctrl+p menu, so a stale number is
# visible in the interface rather than buried in a file nobody opens. MINOR for a
# view or command that did not exist before, PATCH for a fix, MAJOR when the shape
# of the interface changes. The same spirit as the rule mesh-collector's schema.sql
# writes down for the schema, which is the one version in this suite that has never
# been allowed to rot — because it has a consumer.
__version__ = "1.0.0"
