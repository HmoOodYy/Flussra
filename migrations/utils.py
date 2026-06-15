"""
Shared migration utilities.

Provides a dollar-quote-aware SQL statement splitter so that PL/pgSQL
function bodies (which use $$ ... $$ quoting and contain semicolons) can
be included in .sql files without breaking the per-statement Alembic
execution pattern.

Usage in a migration wrapper:
    from pathlib import Path
    from migrations.utils import statements_from_file

    _SQL_FILE = Path(__file__).parent.parent / "sql" / "NNNN_name.sql"

    def upgrade() -> None:
        from alembic import op
        import sqlalchemy as sa
        for stmt in statements_from_file(_SQL_FILE):
            op.execute(sa.text(stmt))
"""
import re
from pathlib import Path

# Dollar-tag pattern: $ then zero or more word chars then $
# e.g. $$  or  $BODY$  or  $func$
_DOLLAR_TAG_RE = re.compile(r"\$[A-Za-z0-9_]*\$")


def statements_from_file(path: Path) -> list[str]:
    """
    Read *path* and split it into individual SQL statements.

    Correctly handles:
    - Line comments (-- ...) stripped before splitting
    - PostgreSQL dollar-quoted blocks ($$ ... $$ or $TAG$ ... $TAG$):
      semicolons inside such blocks are NOT treated as statement terminators

    The conftest applies .sql files directly via psycopg2 cur.execute(), which
    sends the entire file to the PostgreSQL server in one call and handles all
    quoting natively.  This function is only needed by the Alembic Python
    wrapper, which must execute one statement at a time.
    """
    sql = path.read_text(encoding="utf-8")

    stmts: list[str] = []
    current: list[str] = []
    i = 0
    n = len(sql)
    in_dollar_quote = False
    dollar_tag = ""

    while i < n:
        if in_dollar_quote:
            # Only look for the matching closing tag
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m and m.group(0) == dollar_tag:
                current.append(m.group(0))
                i = m.end()
                in_dollar_quote = False
                dollar_tag = ""
            else:
                current.append(sql[i])
                i += 1
            continue

        # Line comment: skip to end of line (keep the newline)
        if sql[i : i + 2] == "--":
            end = sql.find("\n", i)
            if end == -1:
                break
            i = end  # newline consumed in next iteration
            continue

        # Dollar-quote opening
        if sql[i] == "$":
            m = _DOLLAR_TAG_RE.match(sql, i)
            if m:
                current.append(m.group(0))
                dollar_tag = m.group(0)
                i = m.end()
                in_dollar_quote = True
                continue

        # Statement terminator (outside dollar quote)
        if sql[i] == ";":
            stmt = "".join(current).strip()
            if stmt:
                stmts.append(stmt)
            current = []
            i += 1
            continue

        current.append(sql[i])
        i += 1

    # Trailing statement without semicolon
    last = "".join(current).strip()
    if last:
        stmts.append(last)

    return stmts
