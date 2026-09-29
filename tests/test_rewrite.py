import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from relium_check.rewrite import rewrite_sql

ROOT = Path(__file__).resolve().parent.parent
COMPILED = ROOT / "target" / "compiled" / "relium_hotdata_demo" / "models" / "customer_ltv.sql"


@pytest.fixture(scope="session")
def compiled_customer_ltv() -> str:
    dbt = shutil.which("dbt") or str(Path(sys.executable).parent / "dbt")
    subprocess.run(
        [dbt, "compile", "--select", "customer_ltv", "--profiles-dir", str(ROOT)],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return COMPILED.read_text(encoding="utf-8")


def _tables(sql: str) -> set[str]:
    tree = sqlglot.parse_one(sql, read="snowflake")
    ctes = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    return {
        ".".join(p for p in (t.catalog, t.db, t.name) if p)
        for t in tree.find_all(exp.Table)
        if t.name not in ctes or t.db
    }


def test_customer_ltv_tables_rewritten(compiled_customer_ltv):
    out = rewrite_sql(compiled_customer_ltv)
    assert _tables(out) == {"default.public.raw_orders", "default.public.raw_payments"}
    assert '"memory"' not in out and '"main"' not in out


def test_customer_ltv_ctes_untouched(compiled_customer_ltv):
    out = rewrite_sql(compiled_customer_ltv)
    tree = sqlglot.parse_one(out, read="snowflake")
    assert {c.alias_or_name for c in tree.find_all(exp.CTE)} == {"payments", "orders"}
    assert "default.public.orders" not in out
    assert "default.public.payments" not in out


def test_customer_ltv_shape_preserved(compiled_customer_ltv):
    out = rewrite_sql(compiled_customer_ltv)
    select = sqlglot.parse_one(out, read="snowflake")
    assert select.named_selects == ["customer_id", "orders", "ltv"]
    assert select.find(exp.Count).args["this"].__class__ is exp.Distinct


def test_unqualified_and_schema_only_refs():
    out = rewrite_sql("select * from a join s.b on a.id = b.id")
    assert _tables(out) == {"default.public.a", "default.public.b"}
