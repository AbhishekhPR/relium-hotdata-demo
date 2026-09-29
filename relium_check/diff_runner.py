"""Diff a dbt model's output between two git refs on a throwaway Hotdata database.

    python -m relium_check.diff_runner --base-ref main --head-ref pr/fanout-bug \
        --model customer_ltv --key customer_id

Each ref is compiled in its own `git worktree`, rewritten to default.public.*,
and run against the seed CSVs loaded into an instant Hotdata database.

Exit codes: 0 = PASS, 1 = BLOCK (a metric moved by more than the threshold),
2 = the check itself errored (no verdict).
"""

from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
import tempfile
import traceback
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import hotdata
from hotdata.models import (
    CreateDatabaseRequest,
    DatabaseDefaultSchemaDecl,
    DatabaseDefaultTableDecl,
    LoadManagedTableRequest,
    QueryRequest,
)

from relium_check.rewrite import rewrite_sql

SCHEMA = "public"
DEFAULT_THRESHOLD = 0.01

EXIT_PASS, EXIT_BLOCK, EXIT_ERROR = 0, 1, 2


class CheckError(Exception):
    """The check could not run; says nothing about the change under review."""


# --- compile --------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CheckError(f"git {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def compile_model(repo: Path, ref: str, model: str) -> str:
    """dbt-compile `model` at `ref` in a temporary worktree and return the SQL."""
    worktree = Path(tempfile.mkdtemp(prefix="relium-wt-"))
    try:
        _git("worktree", "add", "--detach", "--force", str(worktree), ref, cwd=repo)
        dbt = shutil.which("dbt") or str(Path(sys.executable).parent / "dbt")
        proc = subprocess.run(
            [dbt, "compile", "--select", model, "--profiles-dir", str(worktree),
             "--project-dir", str(worktree)],
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise CheckError(f"dbt compile failed for {ref}:\n{proc.stdout}\n{proc.stderr}")
        matches = list((worktree / "target" / "compiled").rglob(f"{model}.sql"))
        if len(matches) != 1:
            raise CheckError(f"expected one compiled {model}.sql at {ref}, found {len(matches)}")
        return matches[0].read_text(encoding="utf-8")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(worktree)],
                       cwd=repo, capture_output=True)
        shutil.rmtree(worktree, ignore_errors=True)
        subprocess.run(["git", "worktree", "prune"], cwd=repo, capture_output=True)


# --- hotdata --------------------------------------------------------------------


def _client() -> hotdata.ApiClient:
    try:
        api_key = os.environ["HOTDATA_API_KEY"]
        workspace_id = os.environ["HOTDATA_WORKSPACE_ID"]
    except KeyError as e:
        raise CheckError(f"missing environment variable {e.args[0]}") from None
    config = hotdata.Configuration(api_key=api_key, workspace_id=workspace_id)
    return hotdata.ApiClient(config)


def create_seeded_database(client: hotdata.ApiClient, seeds: list[Path]) -> str:
    databases = hotdata.DatabasesApi(client)
    db = databases.create_database(CreateDatabaseRequest(
        name=f"relium-check-{uuid.uuid4().hex[:8]}",
        expires_at="24h",
        schemas=[DatabaseDefaultSchemaDecl(
            name=SCHEMA,
            tables=[DatabaseDefaultTableDecl(name=s.stem) for s in seeds],
        )],
    ))
    return db.id


def load_seeds(client: hotdata.ApiClient, db_id: str, seeds: list[Path]) -> None:
    uploads = hotdata.UploadsApi(client)
    databases = hotdata.DatabasesApi(client)
    for seed in seeds:
        upload = uploads.upload_file(str(seed), content_type="text/csv")
        databases.load_database_table(
            db_id, SCHEMA, seed.stem,
            LoadManagedTableRequest(mode="replace", upload_id=upload.upload_id, format="csv"),
        )


@dataclass
class Result:
    columns: list[str]
    rows: list[list[Any]]


def run_query(client: hotdata.ApiClient, db_id: str, sql: str) -> Result:
    resp = hotdata.QueryApi(client).query(
        QueryRequest(sql=sql, dialect="snowflake"), x_database_id=db_id
    )
    return Result(columns=[c.lower() for c in resp.columns], rows=resp.rows)


# --- compare --------------------------------------------------------------------


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _numeric_columns(res: Result, exclude: str) -> list[str]:
    cols = []
    for i, c in enumerate(res.columns):
        if c == exclude:
            continue
        vals = [r[i] for r in res.rows if r[i] is not None]
        if vals and all(_num(v) is not None for v in vals):
            cols.append(c)
    return cols


def _same(a: Any, b: Any) -> bool:
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return math.isclose(na, nb, rel_tol=1e-9, abs_tol=1e-9)
    return a == b


def _by_key(res: Result, key: str, cols: list[str]) -> dict[Any, list[tuple]]:
    ki = res.columns.index(key)
    idx = [res.columns.index(c) for c in cols]
    out: dict[Any, list[tuple]] = {}
    for r in res.rows:
        out.setdefault(str(r[ki]), []).append(tuple(r[i] for i in idx))
    return out


@dataclass
class Metric:
    name: str
    base: float
    head: float
    delta: float  # relative change; inf when base is 0 and head is not
    flagged: bool
    display: str | None = None  # replaces base/head/delta cells when set


def _rel(base: float, head: float) -> float:
    if base == head:
        return 0.0
    if base == 0:
        return math.inf
    return (head - base) / abs(base)


def compare(base: Result, head: Result, key: str, threshold: float) -> tuple[list[Metric], list[str]]:
    for side, res in (("base", base), ("head", head)):
        if key not in res.columns:
            raise CheckError(f"key column {key!r} not in {side} output: {res.columns}")

    metrics = []

    def add(name: str, b: float, h: float) -> None:
        d = _rel(b, h)
        metrics.append(Metric(name, b, h, d, abs(d) > threshold))

    add("row_count", len(base.rows), len(head.rows))

    base_num = _numeric_columns(base, key)
    head_num = set(_numeric_columns(head, key))
    for c in base_num:
        if c not in head_num:
            continue
        bi, hi = base.columns.index(c), head.columns.index(c)
        b = sum(_num(r[bi]) or 0.0 for r in base.rows)
        h = sum(_num(r[hi]) or 0.0 for r in head.rows)
        add(f"sum({c})", b, h)

    shared = [c for c in base.columns if c in head.columns and c != key]
    bk, hk = _by_key(base, key, shared), _by_key(head, key, shared)
    differing = sorted(
        k for k in bk.keys() | hk.keys()
        if k not in bk or k not in hk
        or len(bk[k]) != len(hk[k])
        or not all(_same(x, y) for rb, rh in zip(sorted(bk[k], key=repr), sorted(hk[k], key=repr))
                   for x, y in zip(rb, rh))
    )
    total_keys = len(bk.keys() | hk.keys())
    share = len(differing) / total_keys if total_keys else 0.0
    metrics.append(Metric(
        f"keys_differing (on {key})", total_keys, len(differing), share, share > threshold,
        display=f"{len(differing)} of {total_keys} ({share:.1%})",
    ))

    return metrics, differing


# --- report ---------------------------------------------------------------------


def _fmt(x: float) -> str:
    return f"{int(x):,}" if float(x).is_integer() else f"{x:,.2f}"


def _fmt_delta(d: float) -> str:
    return "+inf" if math.isinf(d) else f"{d:+.2%}"


def render_report(args: argparse.Namespace, metrics: list[Metric], differing: list[str],
                  base: Result, head: Result) -> tuple[str, bool]:
    block = any(m.flagged for m in metrics)
    lines = [
        f"# Relium check: `{args.model}`",
        "",
        f"`{args.base_ref}` (base) vs `{args.head_ref}` (head), keyed on `{args.key}`, "
        f"flag threshold ±{args.threshold:.0%}.",
        "",
        "| metric | base | head | delta | flag |",
        "|---|---:|---:|---:|:---:|",
    ]
    for m in metrics:
        cells = ("", m.display, "") if m.display else (_fmt(m.base), _fmt(m.head), _fmt_delta(m.delta))
        lines.append(f"| {m.name} | " + " | ".join(cells) + f" | {'**FLAG**' if m.flagged else ''} |")

    if differing:
        cols = [c for c in base.columns if c in head.columns]
        bk, hk = _by_key(base, args.key, cols), _by_key(head, args.key, cols)
        lines += ["", f"### Sample differing keys ({min(5, len(differing))} of {len(differing)})", "",
                  "| side | " + " | ".join(cols) + " |", "|---|" + "---|" * len(cols)]
        for k in differing[:5]:
            for side, rows in (("base", bk.get(k, [])), ("head", hk.get(k, []))):
                for r in rows or [("(missing)",) * len(cols)]:
                    lines.append(f"| {side} | " + " | ".join("" if v is None else str(v) for v in r) + " |")

    lines += ["", f"**VERDICT: {'BLOCK' if block else 'PASS'}**", ""]
    return "\n".join(lines), block


# --- main -----------------------------------------------------------------------


def render_error_report(args: argparse.Namespace, err: BaseException) -> str:
    detail = (str(err).strip().splitlines() or [""])[0][:300]
    return "\n".join([
        f"# Relium check errored: `{args.model}`",
        "",
        "The check could not complete, so **this is not a verdict on the change**. "
        "Re-run it once the problem below is fixed; see the logs for the full traceback.",
        "",
        f"`{type(err).__name__}: {detail}`",
        "",
    ])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base-ref", required=True)
    p.add_argument("--head-ref", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--report", default="relium_report.md")
    args = p.parse_args(argv)  # usage errors exit 2 via argparse
    args.key = args.key.lower()

    report_path = Path(args.report)
    report_path.unlink(missing_ok=True)  # never leave a stale verdict behind
    try:
        report, block = run(args)
    except Exception as e:
        traceback.print_exc()
        report_path.write_text(render_error_report(args, e), encoding="utf-8")
        print("Relium: error (no verdict)", file=sys.stderr)
        return EXIT_ERROR

    print(report)
    report_path.write_text(report, encoding="utf-8")
    return EXIT_BLOCK if block else EXIT_PASS


def run(args: argparse.Namespace) -> tuple[str, bool]:
    repo = Path(_git("rev-parse", "--show-toplevel", cwd=Path.cwd()))
    seeds = sorted((repo / "seeds").glob("*.csv"))
    if not seeds:
        raise CheckError(f"no seed CSVs in {repo / 'seeds'}")

    sql = {ref: rewrite_sql(compile_model(repo, ref, args.model))
           for ref in (args.base_ref, args.head_ref)}

    with _client() as client:
        db_id = create_seeded_database(client, seeds)
        try:
            load_seeds(client, db_id, seeds)
            base = run_query(client, db_id, sql[args.base_ref])
            head = run_query(client, db_id, sql[args.head_ref])
        finally:
            try:
                hotdata.DatabasesApi(client).delete_database(db_id)
            except Exception as e:  # expires_at is the backstop
                print(f"warning: failed to delete Hotdata database {db_id}: {e}", file=sys.stderr)

    metrics, differing = compare(base, head, args.key, args.threshold)
    return render_report(args, metrics, differing, base, head)


if __name__ == "__main__":
    sys.exit(main())
