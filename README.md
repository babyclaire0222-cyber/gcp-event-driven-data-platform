# GCP Event-Driven Data Platform

[![CI](https://github.com/babyclaire0222-cyber/gcp-event-driven-data-platform/actions/workflows/ci.yml/badge.svg)](https://github.com/babyclaire0222-cyber/gcp-event-driven-data-platform/actions/workflows/ci.yml)

A serverless GCP pipeline for finance and ops analysts. Upload a CSV, JSON, Parquet or Excel file, and it's validated, loaded into BigQuery through Pub/Sub and Cloud Functions, and available as Looker Studio dashboards within seconds. No data engineer needed.

## Architecture

```mermaid
flowchart LR
    U(["Analyst"]) -->|HTTPS| IAP["Identity-Aware Proxy"]
    IAP --> CR["Cloud Run: webapp<br/>validate · rate-limit · reports"]
    CR -->|"uploads/{table}.{ext}"| GCS[("Cloud Storage")]
    GCS -->|"OBJECT_FINALIZE notification"| PS[["Pub/Sub topic: uploads"]]
    PS -->|"Eventarc trigger (retry on)"| CF["Cloud Function: ingest"]
    CF -->|"load job (idempotent)"| BQ[("BigQuery dataset")]
    CF -.->|"bad file + reason"| REJ[("GCS rejected/")]
    CR -->|"reports · Looker views · /status"| BQ
    BQ --> LS["Looker Studio"]
```

The web app never loads data itself. It writes the file to GCS and returns `202 Accepted`. Everything after that is event-driven. The UI polls `/status/<table>` until the table is `loaded` or `rejected`.

## Features

| Capability | Where it's implemented |
|---|---|
| Multi-format ingest: CSV, newline-delimited JSON, Parquet, Excel (.xlsx/.xls, converted to CSV) | [`webapp/validation.py`](webapp/validation.py) (`ALLOWED_EXTENSIONS`), [`webapp/main.py`](webapp/main.py) (`index`), [`functions/ingest/main.py`](functions/ingest/main.py) (`SOURCE_FORMATS`) |
| Cloud Storage landing zone with 30-day lifecycle | [`webapp/main.py`](webapp/main.py) (`index`), [`deployment/create_resources.sh`](deployment/create_resources.sh) step 2 |
| Pub/Sub: GCS notification → topic | [`deployment/create_resources.sh`](deployment/create_resources.sh) step 4 |
| Cloud Functions (gen2) Pub/Sub trigger | [`functions/ingest/main.py`](functions/ingest/main.py) (`ingest`), [`deployment/deploy.sh`](deployment/deploy.sh) (`--trigger-topic --retry`) |
| BigQuery processing: load jobs + 5 templated finance reports | [`functions/ingest/main.py`](functions/ingest/main.py) (`load_job_config`), [`webapp/main.py`](webapp/main.py) (`REPORTS`, `render_report_sql`) |
| Cloud Run app layer behind IAP | [`webapp/Dockerfile`](webapp/Dockerfile), [`deployment/deploy.sh`](deployment/deploy.sh) (`--iap --no-allow-unauthenticated`) |
| Looker Studio: one BigQuery view per report | [`webapp/main.py`](webapp/main.py) (`publish_looker_views_for_table`) |
| Docker Compose, local + prod | [`docker-compose.yml`](docker-compose.yml) (hot reload + Redis), [`docker-compose.prod.yml`](docker-compose.prod.yml) (Cloud Run image, read-only FS) |
| Rate limiting (per user, Redis or in-memory) | [`webapp/ratelimit.py`](webapp/ratelimit.py) |
| Input validation (two layers) | [`webapp/validation.py`](webapp/validation.py), [`functions/ingest/main.py`](functions/ingest/main.py) (`parse_object_name`, `reject`) |
| Tests + CI (lint, 54 unit tests, Docker build + smoke test) | [`tests/`](tests), [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |

## Design decisions

- **Why Pub/Sub sits between GCS and the loader.** An explicit topic decouples the upload from the load. The web app replies in milliseconds instead of blocking on a BigQuery job. Pub/Sub adds at-least-once delivery with retries, the notification is scoped to `uploads/` so the function's own writes to `rejected/` don't re-trigger it, and a second consumer (data-quality checks, audit logging) can subscribe later without touching the upload path. A direct GCS trigger would also use Pub/Sub internally, but it hides the topic, so you can't add subscribers or inspect it.
- **Idempotent loads under at-least-once delivery.** Every BigQuery load job ID is `ingest_<table>_<object generation>`. If Pub/Sub delivers the same notification twice, the second `load_table_from_uri` gets `409 Conflict`, and the function waits on the existing job instead of loading twice. A re-upload creates a new object generation, so it gets a new job.
- **Validation happens twice and separates permanent from transient errors.** The web app rejects bad files before they reach GCS:
  - extension allow-list and `secure_filename` (stops path traversal)
  - a 50 MB cap (Flask returns 413)
  - magic-byte checks (a CSV renamed to `.parquet` fails)
  - a CSV header check (≥ 2 named, unique columns)
  - a newline-delimited JSON check

  The function validates again, because anything with bucket access can write to `uploads/`. If a file can *never* load (bad name, empty, or a BigQuery parse error with `max_bad_records=0`), the function moves it to `rejected/`, records the reason in object metadata, and acks the message so it isn't retried forever. Transient errors are raised instead, so Pub/Sub retries them.
- **Rate limiting is keyed on identity, not IP.** Limits are per IAP-authenticated email, because `X-Forwarded-For` is set by the client and easy to spoof (see `test_spoofed_forwarded_for_does_not_reset_limit`). With `REDIS_URL` set, a fixed-window counter in Redis (`INCR` + `EXPIRE`) is shared by every process. Without Redis, each process keeps a sliding-window log in memory, and gunicorn runs as one process with 8 threads so that limit is exact per instance. Over-limit requests get `429` with a `Retry-After` header.
- **SQL injection is prevented, not escaped.** Table names must match `^[a-zA-Z_][a-zA-Z0-9_]{0,127}$` before they reach any query. Report column names are read from the table's BigQuery schema, never from the request.

## Run locally

Prerequisites: Docker, and the `gcloud` CLI logged in to a dev project where `create_resources.sh` has been run. The pipeline itself runs in GCP, so a local upload goes through the same Pub/Sub → function → BigQuery path as production.

```bash
gcloud auth application-default login
cp .env.example .env          # set GCP_PROJECT, BUCKET_NAME, DEV_USER_EMAIL
docker compose up --build     # http://localhost:8080, hot reload, Redis rate limiting
```

On Windows, first run `export GCLOUD_CONFIG_DIR="$APPDATA/gcloud"` (Git Bash) so Compose can find your credentials.

To run the exact image Cloud Run uses (gunicorn, non-root, read-only filesystem):

```bash
docker compose -f docker-compose.prod.yml up --build
```

Tests don't need GCP:

```bash
pip install -r requirements-dev.txt
ruff check . && pytest
```

Try `sample_data/finance_6months.csv` (loads and powers the reports) and `sample_data/bad_rows.json` (passes the web app's first-line check, then BigQuery rejects it and it shows up as `rejected` with the reason).

## Deploy

```bash
export PROJECT_ID=your-project IAP_USER=you@gmail.com
./deployment/create_resources.sh
```

What it does, in order:

1. Enables Cloud Run, Cloud Functions, Cloud Build, Artifact Registry, Eventarc, Pub/Sub, Cloud Storage, BigQuery and IAP.
2. Creates the bucket (`$PROJECT_ID-uploads`, `asia-southeast1`) with a 30-day delete rule on `uploads/` and `rejected/`.
3. Creates the BigQuery dataset `analysis_dataset` in the **same region** as the bucket.
4. Creates the Pub/Sub topic `uploads`, grants the GCS service agent `pubsub.publisher`, and adds an `OBJECT_FINALIZE` notification on the `uploads/` prefix.
5. Creates two service accounts, one per component (roles below).
6. Runs [`deploy.sh`](deployment/deploy.sh) to deploy the function and the Cloud Run service with `--iap`, then grants `IAP_USER` access.

Later code-only deploys: run `./deployment/deploy.sh`, or run the manual **Deploy** GitHub Actions workflow.

**IAM roles**

| Principal | Roles |
|---|---|
| `webapp-sa` (Cloud Run) | `storage.objectUser` on the bucket, `bigquery.dataEditor`, `bigquery.jobUser` |
| `ingest-sa` (Function + trigger) | `storage.objectUser` on the bucket, `bigquery.dataEditor`, `bigquery.jobUser`, `eventarc.eventReceiver`, `run.invoker` |
| GCS service agent | `pubsub.publisher` on the `uploads` topic |
| IAP service agent | `run.invoker` on the Cloud Run service |
| You (`IAP_USER`) | `iap.httpsResourceAccessor` on the Cloud Run service |
| Person running the script | `owner`, or `editor` + `resourcemanager.projectIamAdmin` |

If your project isn't in a Google Workspace organization, IAP may ask you to configure the OAuth consent screen the first time.

**Rough monthly cost** (portfolio-level traffic, asia-southeast1 list prices): **under $1/month**.

- Cloud Run, Cloud Functions, Pub/Sub, Cloud Build and BigQuery (10 GB storage, 1 TiB queries) all stay inside their free tiers.
- The only line items are GCS storage (~$0.02/GB) and Artifact Registry image storage beyond 0.5 GB (~$0.10/GB).
- Adding Memorystore for shared rate limiting on Cloud Run would add about $35/month, so the deployed stack doesn't include it (see limitations).

## Teardown

```bash
PROJECT_ID=your-project ./deployment/teardown.sh
```

This deletes the Cloud Run service, the function (and its Eventarc trigger), the bucket and all files, the BigQuery dataset, the topic, both service accounts, and the build image repositories. It asks you to type the project ID first. To remove *everything*, run `gcloud projects delete $PROJECT_ID`.

## Screenshots

**Looker Studio dashboard** built on the `finance_6months__*_v` views:

![Looker Studio dashboard](docs/screenshots/looker-dashboard.png)

**BigQuery query** against the loaded table:

![BigQuery query result](docs/screenshots/bigquery-query.png)

## What I'd do next

1. **Shared rate limiting in production.** The deployed Cloud Run service has no Redis, so limits apply per instance. With `--max-instances=3`, one user could in theory make 3× the configured limit. The fix is to point `REDIS_URL` at Memorystore through a VPC connector, which I skipped to keep the demo under $1/month.
2. **Verify the IAP JWT in the app.** The app trusts `X-Goog-Authenticated-User-Email` because the service only accepts traffic through IAP. Checking the signed `X-Goog-IAP-JWT-Assertion` would protect against a future misconfiguration, such as someone adding `--allow-unauthenticated`.
3. **Operational hardening.** Items still missing:
   - a dead-letter topic and alerting for messages that keep failing
   - schema-drift checks, since `WRITE_TRUNCATE` means a re-upload silently replaces the table
   - Workload Identity Federation for the deploy workflow instead of a JSON service-account key
