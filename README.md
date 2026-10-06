# SemantiCI

A business-correctness release gate for CI/CD pipelines.

Ordinary pipelines check that the build works, tests pass and services respond.
SemantiCI checks whether the application produced the correct **business result**,
for example that every successful payment has exactly one order. If a critical
business invariant is violated, the release is blocked and the violation is turned
into a regression test for future pipeline runs.

## Prototype scope

This is a working prototype, not the full platform.

| Supported now | Not built yet (future work) |
|---|---|
| Project submitted as a Git URL or local folder | Automatic build of any language without configuration |
| Apps that include a `semantici.yml` (start command, health URL, database path) | Driving the application through its UI |
| SQLite application databases | PostgreSQL / MySQL |
| Workflows executed through HTTP | Kubernetes deployment, monitoring |
| Candidate invariants from an LLM, or from schema rules when no key is set | Evaluation on many applications |

SemantiCI runs the start command from the submitted repository on the local
machine, so only submit repositories you trust.

## How it works

1. **Submit** a Git URL or folder in the web UI.
2. **Analyze**: SemantiCI starts the app, reads its OpenAPI document, database schema and source code.
3. **Propose**: candidate business invariants are generated. Each is a SQL query that returns the records violating the rule.
4. **Review**: a person approves, edits or rejects every candidate. Only approved invariants are release gates.
5. **Execute**: the approved workflows are run against a fresh copy of the app.
6. **Verify**: every approved invariant is checked against the app's database (read-only).
7. **Decide**: PASS, or BLOCKED when a critical invariant is violated.
8. **Generate**: each violation becomes a pytest file in `<app>/business_checks/`.
9. **CI gate**: `python -m semantici.cli gate <app>` runs the approved suite in GitHub Actions and fails the pipeline.

## Run it

```bash
pip install -r requirements.txt
python -m uvicorn semantici.web:app --port 8000
```

Open http://localhost:8000.

Optional: copy `.env.example` to `.env` and add an LLM API key to get LLM-proposed
invariants. Without a key, candidates come from schema rules.

## Demo script

1. Submit the project: source `https://github.com/harsh-15-2006/SemantiCI` (or this folder), application folder `demo-apps/shop`.
2. Click **Analyze**. Review the candidates and approve them.
3. Add these two invariants under "Add your own invariant" (severity: critical):

   Stock conservation:
   ```sql
   SELECT p.id, p.name, p.initial_stock, p.stock, COALESCE(SUM(oi.qty),0) AS units_sold
   FROM products p LEFT JOIN order_items oi ON oi.product_id = p.id
   GROUP BY p.id HAVING p.stock <> p.initial_stock - COALESCE(SUM(oi.qty),0)
   ```
   No duplicate payment:
   ```sql
   SELECT checkout_ref, COUNT(*) AS successful_payments, SUM(amount) AS total_charged
   FROM payments WHERE status = 'SUCCESS' GROUP BY checkout_ref HAVING COUNT(*) > 1
   ```
4. Click **Run verification** with the environment box empty: release status is PASS.
5. Run again with `SHOP_BUG=skip_order`: release status is BLOCKED, with the violating payment records and a generated regression test.
6. Show that the ordinary unit tests still pass with the bug:
   ```bash
   cd demo-apps/shop
   SHOP_BUG=skip_order python -m pytest tests -q
   ```
7. Show the CI gate from the command line:
   ```bash
   SHOP_BUG=skip_order python -m semantici.cli gate demo-apps/shop
   ```
8. On GitHub: Actions -> SemantiCI Pipeline -> Run workflow -> choose a bug. The pipeline turns red at the release gate.

## Bugs available in the demo shop

| `SHOP_BUG` | What goes wrong | HTTP status |
|---|---|---|
| `none` | Nothing | 200 |
| `skip_order` | Payment succeeds, no order is created | 200 |
| `double_charge` | Customer is charged twice for one checkout | 200 |
| `no_stock_update` | Order is created, inventory is not reduced | 200 |

## Layout

```
semantici/            the platform (web UI, analyzer, runner, verifier, test generator, CLI gate)
demo-apps/shop/       sample e-commerce application with switchable business bugs
  semantici.yml       how to start the app and where its database is
  business_checks/    approved suite + generated regression tests (used by CI)
.github/workflows/    GitHub Actions pipeline with the release gate
```
