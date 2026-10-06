# SemantiCI

A business-correctness release gate for CI/CD pipelines.

Ordinary pipelines check that the build works, tests pass and services respond.
SemantiCI checks whether the application produced the correct **business result**,
for example that every successful payment has exactly one order. If a critical
business invariant is violated, the release is blocked and the violation is turned
into a regression test for future pipeline runs.

## Prototype scope

This is a working prototype, not the full platform.

| Supported now | Not supported (future work) |
|---|---|
| Any Git URL or local folder; no configuration file needed | Languages other than Python and Node.js |
| Python web apps (FastAPI, Flask, Django) and Node.js web apps (Express) | Apps whose data is in PostgreSQL, MySQL, MongoDB, Firebase or memory |
| Apps that keep their data in SQLite (the file is found automatically) | Apps that need real secrets, paid services or third-party logins |
| Workflows through HTTP, including token and cookie logins | Driving the application through its browser UI |
| Run configuration, workflows and invariants proposed by an LLM, confirmed by a person | Old projects whose dependencies no longer install |

### Auto-onboarding

When a repository has no `semantici.yml`, SemantiCI reads the repository and proposes how to
install and start it. The user confirms or edits that proposal, SemantiCI installs the
dependencies in an isolated environment and starts the app once. If that fails, the error is
sent back to the LLM for a corrected proposal. Proposed workflows are dry-run against the
running app and repaired once if a step fails.

Tested on public repositories on 2026-10-07 (inside the Docker container):

| Repository | Stack | Result |
|---|---|---|
| madhulathahl/Basic-Banking-Application | FastAPI, SQLAlchemy, JWT | Full flow completed |
| vakitisahana-alt/E-Commerce-REST-API | Flask | Full flow completed |
| IMRANDIL/Express_SQLite_Rest_Api | Node.js, Express, Sequelize | Full flow completed |
| masfranzhuo/sequalize-express-SQLite | Node.js, 2017 dependencies | Failed: its sqlite3 package does not build on Node 20 |

SemantiCI runs code from the submitted repository. Run SemantiCI in Docker so that code is
contained, and only submit repositories you are willing to run.

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

### Run it in Docker

```bash
docker compose up --build
```

Open http://localhost:8000. Inside the container the repository is at `/app`, so submit
source `/app` with application folder `demo-apps/shop` or `demo-apps/bank` (or submit a Git URL).

Optional: copy `.env.example` to `.env` and add an LLM API key to get LLM-proposed
invariants. Without a key, candidates come from schema rules.

## Demo script

1. Submit the project: source `https://github.com/harsh-15-2006/SemantiCI` (or this folder), application folder `demo-apps/shop`.
2. Click **Analyze**. Review the candidates and approve them.
3. With an LLM key the candidates already cover stock and duplicate payments. Without a key, add these two under "Add your own invariant" (severity: critical):

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
8. On GitHub: Actions -> SemantiCI Pipeline -> Run workflow -> choose a shop or bank bug. The pipeline builds the Docker image and turns red at the release gate.

## Bugs available in the demo shop

| `SHOP_BUG` | What goes wrong | HTTP status |
|---|---|---|
| `none` | Nothing | 200 |
| `skip_order` | Payment succeeds, no order is created | 200 |
| `double_charge` | Customer is charged twice for one checkout | 200 |
| `no_stock_update` | Order is created, inventory is not reduced | 200 |

## Bugs available in the demo bank

| `BANK_BUG` | What goes wrong | HTTP status |
|---|---|---|
| `none` | Nothing | 200 |
| `no_credit` | Sender is debited, receiver is never credited | 200 |
| `double_debit` | Sender is debited twice for one transfer | 200 |
| `overdraft` | A transfer larger than the balance is allowed | 200 |

## Layout

```
semantici/            the platform (web UI, analyzer, runner, verifier, test generator, CLI gate)
demo-apps/shop/       sample e-commerce application (FastAPI) with switchable business bugs
demo-apps/bank/       sample banking application (Flask) with switchable business bugs
  semantici.yml       how to start the app and where its database is
  business_checks/    approved suite + generated regression tests (used by CI)
.github/workflows/    GitHub Actions pipeline with the release gate
```
