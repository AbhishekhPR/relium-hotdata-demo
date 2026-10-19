# relium-hotdata-demo

A dbt project (`models/customer_ltv.sql` over the CSVs in `seeds/`) used to demo the Relium check.

## Relium check

On every pull request that touches `models/**`, the `Relium check` workflow
(`.github/workflows/relium-check.yml`) compares the output of `customer_ltv` on the
base branch against the PR:

1. Compiles the model at both refs with `dbt compile` (each in its own `git worktree`)
   and rewrites table references to `default.public.*`.
2. Creates a throwaway Hotdata database (expires after 24h, deleted when the run ends),
   loads `seeds/*.csv` into `public.<name>`, and runs both queries.
3. Compares row count, the sum of every numeric column, and how many `customer_id`
   rows changed. Anything that moves by more than 1% is flagged.
4. Posts the report as a PR comment, updated in place on every re-run.

| Exit code | Meaning | Check status |
|---|---|---|
| 0 | PASS: nothing moved more than 1% | passes |
| 1 | BLOCK: at least one metric flagged | fails as **Relium: BLOCK** |
| 2 | The check itself errored (no verdict on the PR) | fails as **Relium: error** |

### Secrets

Set these under **Settings → Secrets and variables → Actions**:

- `HOTDATA_API_KEY`
- `HOTDATA_WORKSPACE_ID`

Pull requests from forks don't receive secrets, so the check errors on those.

### Run locally

```sh
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Windows: .venv\Scripts\pip
export HOTDATA_API_KEY=... HOTDATA_WORKSPACE_ID=...                  # PowerShell: $env:HOTDATA_API_KEY = "..."
python -m relium_check.diff_runner --base-ref main --head-ref pr/fanout-bug \
    --model customer_ltv --key customer_id
```

The report is printed and written to `relium_report.md` (gitignored). Options:
`--threshold 0.05` changes the flag threshold, and `--report PATH` changes where the report is written.
Your working tree is never touched; only committed refs are compared.

Run the unit tests with `pytest`.
