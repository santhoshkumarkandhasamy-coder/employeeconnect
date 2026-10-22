"""Helper: run a block of raw SQL as individual statements (works online and in --sql mode)."""
import sqlalchemy as sa
from alembic import op


def split_sql(script: str) -> list[str]:
    """Split on ';' + newline, ignoring semicolons inside $$ ... $$ bodies."""
    out, buf, in_dollar = [], [], False
    for line in script.splitlines():
        buf.append(line)
        closed_dollar = False
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
            closed_dollar = not in_dollar
        if not in_dollar and (closed_dollar or line.rstrip().endswith(";")):
            stmt = "\n".join(buf).strip()
            if stmt:
                out.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        out.append(tail)
    return [s.rstrip().rstrip(";").rstrip() for s in out]


def run(script: str) -> None:
    for stmt in split_sql(script):
        op.execute(sa.DDL(stmt))
