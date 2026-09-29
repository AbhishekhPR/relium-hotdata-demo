"""Rewrite table references in dbt-compiled SQL to Hotdata's default.public namespace."""

from __future__ import annotations

import sys

import sqlglot
from sqlglot import exp

TARGET_CATALOG = "default"
TARGET_SCHEMA = "public"


def rewrite_sql(sql: str, read: str = "duckdb", write: str = "snowflake") -> str:
    """Point every physical table reference at default.public.<table_name>.

    CTE references (unqualified names matching a CTE alias) are left alone,
    since rewriting them would break the query.
    """
    tree = sqlglot.parse_one(sql, read=read)
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}

    for table in tree.find_all(exp.Table):
        if not table.name:
            continue  # e.g. table functions
        if not table.args.get("db") and table.name.lower() in cte_names:
            continue
        table.set("catalog", exp.to_identifier(TARGET_CATALOG))
        table.set("db", exp.to_identifier(TARGET_SCHEMA))
        table.set("this", exp.to_identifier(table.name))

    return tree.sql(dialect=write, pretty=True)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else None
    sql = open(path, encoding="utf-8").read() if path else sys.stdin.read()
    print(rewrite_sql(sql))


if __name__ == "__main__":
    main()
