"""Initial schema — all 42 tables, 7 views, 2 functions, 108 indexes, 328 checks, 128 FKs.

Validated against a live PostgreSQL 18 cluster with zero execution errors.

Revision ID: 0001
Revises:
Create Date: 2026-05-28
"""
from pathlib import Path
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Absolute path to the SQL file stored next to this migrations directory
_SQL_FILE = Path(__file__).parent.parent / "sql" / "0001_initial_schema.sql"


def _split_statements(sql: str) -> list[str]:
    """
    Split a PostgreSQL SQL script into individual statements.

    Correctly handles:
    - Dollar-quoted strings  ($$...$$, $tag$...$tag$) which may contain semicolons
    - Single-line comments   (-- ...)
    - Block comments         (/* ... */)
    - Regular string literals ('...')

    Returns a list of non-empty statement strings (without trailing semicolons,
    but the caller may add them back — here we keep them for clarity in logs).
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)

    in_single_quote = False
    in_line_comment = False
    in_block_comment = False
    dollar_tag: str | None = None  # e.g. '$$' or '$func$'

    while i < n:
        ch = sql[i]

        # ------------------------------------------------------------------ #
        # Single-line comment  --  (ignored inside quotes / dollar blocks)
        # ------------------------------------------------------------------ #
        if not in_single_quote and dollar_tag is None and not in_block_comment:
            if not in_line_comment and ch == '-' and i + 1 < n and sql[i + 1] == '-':
                in_line_comment = True
                buf.append(ch)
                i += 1
                continue

        if in_line_comment:
            buf.append(ch)
            if ch == '\n':
                in_line_comment = False
            i += 1
            continue

        # ------------------------------------------------------------------ #
        # Block comment  /* ... */
        # ------------------------------------------------------------------ #
        if not in_single_quote and dollar_tag is None and not in_line_comment:
            if not in_block_comment and ch == '/' and i + 1 < n and sql[i + 1] == '*':
                in_block_comment = True
                buf.append(ch)
                buf.append(sql[i + 1])
                i += 2
                continue
            if in_block_comment and ch == '*' and i + 1 < n and sql[i + 1] == '/':
                in_block_comment = False
                buf.append(ch)
                buf.append(sql[i + 1])
                i += 2
                continue

        if in_block_comment:
            buf.append(ch)
            i += 1
            continue

        # ------------------------------------------------------------------ #
        # Single-quoted string literals  '...'  ('' is an escaped quote)
        # ------------------------------------------------------------------ #
        if dollar_tag is None and not in_line_comment and not in_block_comment:
            if ch == "'":
                in_single_quote = not in_single_quote
                buf.append(ch)
                i += 1
                continue

        if in_single_quote:
            buf.append(ch)
            i += 1
            continue

        # ------------------------------------------------------------------ #
        # Dollar-quoting  $$...$$  or  $tag$...$tag$
        # ------------------------------------------------------------------ #
        if ch == '$' and not in_line_comment and not in_block_comment:
            # Find the closing '$' of the tag
            j = sql.find('$', i + 1)
            if j != -1:
                tag = sql[i: j + 1]  # e.g. '$$' or '$body$'
                if dollar_tag is None:
                    # Opening tag
                    dollar_tag = tag
                    buf.append(tag)
                    i = j + 1
                    continue
                elif dollar_tag == tag:
                    # Closing tag
                    dollar_tag = None
                    buf.append(tag)
                    i = j + 1
                    continue

        if dollar_tag is not None:
            buf.append(ch)
            i += 1
            continue

        # ------------------------------------------------------------------ #
        # Statement terminator  ;
        # ------------------------------------------------------------------ #
        if ch == ';':
            buf.append(ch)
            stmt = ''.join(buf).strip()
            if stmt and stmt != ';':
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    # Trailing statement with no semicolon
    remainder = ''.join(buf).strip()
    if remainder:
        statements.append(remainder)

    return statements


def upgrade() -> None:
    sql = _SQL_FILE.read_text(encoding="utf-8")
    # asyncpg uses the extended query protocol which rejects multi-statement
    # scripts.  Execute each statement individually.
    for stmt in _split_statements(sql):
        op.execute(sa.text(stmt))


def downgrade() -> None:
    """
    Drop every application schema in reverse dependency order.
    WARNING: This destroys all payroll data — use only in development.
    """
    op.execute(sa.text("DROP SCHEMA IF EXISTS app CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS audit CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS review CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS integration CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS import CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS payroll CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS core CASCADE"))
    op.execute(sa.text("DROP SCHEMA IF EXISTS sec CASCADE"))
    op.execute(sa.text("DROP EXTENSION IF EXISTS pgcrypto CASCADE"))
