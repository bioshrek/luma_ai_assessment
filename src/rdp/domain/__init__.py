"""The domain layer: entities, value objects and pure services.

Zero IO. This package must not import sqlite3, pyarrow, yaml, typer or any adapter — the rule
is enforced by `import-linter`, not by good intentions.
"""

from __future__ import annotations

from rdp.domain.episode import SCHEMA_VERSION

__all__ = ["SCHEMA_VERSION"]
