"""Cloud Run app layer: authenticated upload to GCS, reports and Looker views over BigQuery.

Ingestion is event-driven and does NOT happen here: this app writes the file to
gs://$BUCKET_NAME/uploads/, a GCS notification publishes to Pub/Sub, and the
`ingest` Cloud Function loads it into BigQuery (see functions/ingest/main.py).
"""
import csv
import io
import logging
import os
from functools import lru_cache, wraps

import pandas as pd
from flask import Flask, g, jsonify, render_template, request, send_file
from google.api_core.exceptions import NotFound
from google.cloud import bigquery, storage

import metrics
from ratelimit import rate_limit
from validation import (
    ALLOWED_EXTENSIONS,
    HEAD_BYTES,
    MAX_UPLOAD_BYTES,
    ValidationError,
    split_filename,
    validate_content,
    validate_table_name,
)

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES  # Flask returns 413 above this
metrics.init_app(app)

PROJECT_ID = os.environ.get("GCP_PROJECT", "")
BUCKET_NAME = os.environ.get("BUCKET_NAME", "")
BIGQUERY_DATASET = os.environ.get("BIGQUERY_DATASET", "analysis_dataset")
UPLOAD_PREFIX = "uploads/"
REJECTED_PREFIX = "rejected/"


# Clients are created on first use so the app (and tests) can import without credentials.
@lru_cache(maxsize=1)
def gcs() -> storage.Client:
    return storage.Client(project=PROJECT_ID or None)


@lru_cache(maxsize=1)
def bq() -> bigquery.Client:
    return bigquery.Client(project=PROJECT_ID or None)


# ===============================
# Security headers
# ===============================
@app.after_request
def add_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; connect-src 'self'"
    )
    return response


@app.errorhandler(413)
def too_large(_):
    # The body is never parsed (that is what raised 413), so the format is unknown.
    metrics.UPLOADS.labels("unknown", "rejected_too_large").inc()
    mb = MAX_UPLOAD_BYTES // (1024 * 1024)
    return jsonify({"success": False, "error": f"File too large (max {mb} MB)"}), 413


# ===============================
# IAP auth
# ===============================
@app.before_request
def read_iap_identity():
    """Cloud Run is deployed with --iap and --no-allow-unauthenticated, so every
    request has passed IAP, which sets X-Goog-Authenticated-User-Email."""
    raw = request.headers.get("X-Goog-Authenticated-User-Email", "")
    g.user_email = raw.split(":", 1)[1] if raw.startswith("accounts.google.com:") else None
    # Local docker-compose has no IAP in front. K_SERVICE is set by Cloud Run,
    # so this override can never apply to a deployed service.
    if g.user_email is None and not os.environ.get("K_SERVICE"):
        g.user_email = os.environ.get("DEV_USER_EMAIL") or None


def require_user(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.user_email:
            return jsonify({"success": False, "error": "Unauthorized"}), 401
        return fn(*args, **kwargs)

    return wrapper


# ===============================
# Upload -> GCS (triggers the pipeline)
# ===============================
def _format_label(uploaded) -> str:
    """Metric label for the upload's format; unknown extensions collapse to 'other'."""
    ext = os.path.splitext(getattr(uploaded, "filename", "") or "")[1].lower()
    return ext.lstrip(".") if ext in ALLOWED_EXTENSIONS else "other"


CONTENT_TYPES = {
    ".csv": "text/csv",
    ".json": "application/x-ndjson",
    ".parquet": "application/vnd.apache.parquet",
}


@app.route("/", methods=["GET", "POST"])
@require_user
@rate_limit()
def index():
    if request.method == "GET":
        return render_template("index.html")

    uploaded = request.files.get("file")
    if uploaded is None:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    fmt = _format_label(uploaded)
    try:
        table_name, ext = split_filename(uploaded.filename)
        head = uploaded.stream.read(HEAD_BYTES)
        uploaded.stream.seek(0)
        validate_content(head, ext)
        if ext in (".xlsx", ".xls"):
            # Convert Excel here so the ingest function only handles BigQuery-native formats.
            data = pd.read_excel(uploaded.stream).to_csv(index=False).encode("utf-8")
            ext = ".csv"
        else:
            data = uploaded.stream
    except ValidationError as e:
        logger.info("Rejected upload %r from %s: %s", uploaded.filename, g.user_email, e)
        metrics.UPLOADS.labels(fmt, "rejected_validation").inc()
        return jsonify({"success": False, "error": str(e)}), 400
    except Exception:
        logger.exception("Could not read upload %r", uploaded.filename)
        metrics.UPLOADS.labels(fmt, "rejected_validation").inc()
        return jsonify({"success": False, "error": "Could not read file"}), 400

    object_name = f"{UPLOAD_PREFIX}{table_name}{ext}"
    try:
        blob = gcs().bucket(BUCKET_NAME).blob(object_name)
        blob.metadata = {"uploaded-by": g.user_email, "original-name": uploaded.filename}
        if isinstance(data, bytes):
            blob.upload_from_string(data, content_type=CONTENT_TYPES[ext])
        else:
            blob.upload_from_file(data, content_type=CONTENT_TYPES[ext])
    except Exception:
        logger.exception("GCS upload failed for gs://%s/%s", BUCKET_NAME, object_name)
        metrics.UPLOADS.labels(fmt, "error").inc()
        return jsonify({"success": False, "error": "Storage is unavailable; please retry."}), 502
    logger.info("Uploaded gs://%s/%s for %s", BUCKET_NAME, blob.name, g.user_email)
    metrics.UPLOADS.labels(fmt, "accepted").inc()
    metrics.UPLOAD_BYTES.observe(request.content_length or 0)

    return jsonify({
        "success": True,
        "status": "processing",
        "message": f"Uploaded {uploaded.filename}; loading into BigQuery table '{table_name}'",
        "table": table_name,
    }), 202


@app.route("/status/<table>")
@require_user
def status(table):
    """Poll ingestion result: loaded (table exists), rejected, or processing."""
    try:
        table = validate_table_name(table)
    except ValidationError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    try:
        t = bq().get_table(f"{PROJECT_ID}.{BIGQUERY_DATASET}.{table}")
        return jsonify({"success": True, "status": "loaded", "rows": t.num_rows})
    except NotFound:
        pass

    for blob in gcs().list_blobs(BUCKET_NAME, prefix=f"{REJECTED_PREFIX}{table}."):
        reason = (blob.metadata or {}).get("rejection-reason", "rejected by ingest")
        return jsonify({"success": True, "status": "rejected", "reason": reason})

    return jsonify({"success": True, "status": "processing"})


def _csv_response(result, filename: str):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([f.name for f in result.schema])
    for row in result:
        writer.writerow(list(row.values()))
    return send_file(io.BytesIO(out.getvalue().encode("utf-8")), mimetype="text/csv",
                     as_attachment=True, download_name=filename)


@app.route("/download_bq")
@require_user
@rate_limit()
def download_bq():
    """Export a loaded table as CSV (capped at 10k rows)."""
    try:
        table = validate_table_name(request.args.get("table", ""))
        rows = bq().list_rows(f"{PROJECT_ID}.{BIGQUERY_DATASET}.{table}", max_results=10_000)
        return _csv_response(rows, f"{table}.csv")
    except ValidationError as e:
        return str(e), 400
    except NotFound:
        return "Table not found (still processing?)", 404


@app.route("/whoami")
@require_user
def whoami():
    return jsonify({"success": True, "email": g.user_email})


@app.route("/healthz")
def healthz():
    return "ok", 200


# ===============================
# Finance reports (templated BigQuery SQL)
# ===============================
def _pick_column(cols: dict, candidates, required=True):
    """First existing column (case-insensitive) from candidates."""
    for c in candidates:
        if c in cols:
            return cols[c]
    if required:
        raise ValidationError(f"Could not find any of columns: {candidates}")
    return None


def _detect_finance_columns(table_fq: str) -> dict:
    """Map report roles to real column names. Names come from the table schema,
    never from the request, so they are safe to interpolate."""
    cols = {f.name.lower(): f.name for f in bq().get_table(table_fq).schema}
    return {
        "department": _pick_column(cols, ["department", "dept", "cost_center", "costcentre", "cost_centre"]),
        "amount": _pick_column(cols, ["amount", "total_amount", "spend", "cost", "value", "amt"]),
        "date": _pick_column(
            cols, ["date", "txn_date", "transaction_date", "post_date", "doc_date", "month", "period"]
        ),
        "expense_type": _pick_column(cols, ["expense_type", "category", "type", "gl_code"], required=False),
    }


REPORTS = {
    "dept_totals": {
        "label": "Total spend per department",
        "sql": """
        SELECT `{department}` AS department, SUM(`{amount}`) AS total_spent
        FROM `{table_fq}`
        GROUP BY department
        ORDER BY total_spent DESC
        """,
    },
    "monthly_trend": {
        "label": "Monthly spend trend",
        "sql": """
        SELECT FORMAT_DATE('%Y-%m', DATE(`{date}`)) AS month, SUM(`{amount}`) AS total_spent
        FROM `{table_fq}`
        GROUP BY month
        ORDER BY month
        """,
    },
    "top_expense_types": {
        "label": "Top 5 expense categories",
        "sql": """
        SELECT {expense_type} AS expense_type, SUM(`{amount}`) AS total_spent
        FROM `{table_fq}`
        GROUP BY expense_type
        ORDER BY total_spent DESC
        LIMIT 5
        """,
    },
    "dept_month_matrix": {
        "label": "Department spend by month",
        "sql": """
        SELECT FORMAT_DATE('%Y-%m', DATE(`{date}`)) AS month,
               `{department}` AS department,
               SUM(`{amount}`) AS total_spent
        FROM `{table_fq}`
        GROUP BY month, department
        ORDER BY month, department
        """,
    },
    "avg_monthly_by_dept": {
        "label": "Average monthly spend per department",
        "sql": """
        WITH monthly AS (
          SELECT `{department}` AS department,
                 FORMAT_DATE('%Y-%m', DATE(`{date}`)) AS month,
                 SUM(`{amount}`) AS monthly_spent
          FROM `{table_fq}`
          GROUP BY department, month
        )
        SELECT department, AVG(monthly_spent) AS avg_monthly_spent
        FROM monthly
        GROUP BY department
        ORDER BY avg_monthly_spent DESC
        """,
    },
}


def render_report_sql(report_id: str, table_name: str) -> str:
    table_fq = f"{PROJECT_ID}.{BIGQUERY_DATASET}.{validate_table_name(table_name)}"
    cols = _detect_finance_columns(table_fq)
    return REPORTS[report_id]["sql"].format(
        table_fq=table_fq,
        department=cols["department"],
        amount=cols["amount"],
        date=cols["date"],
        expense_type=f"`{cols['expense_type']}`" if cols["expense_type"] else "NULL",
    )


@app.route("/reports")
@require_user
def list_reports():
    return jsonify({"reports": [{"id": k, "label": v["label"]} for k, v in REPORTS.items()]})


@app.route("/run_report", methods=["POST"])
@require_user
@rate_limit()
def run_report():
    """Body: {"report": "<id>", "table": "<table>", "limit": 1000}"""
    data = request.get_json(silent=True) or {}
    report_id = data.get("report")
    if report_id not in REPORTS:
        return jsonify({"success": False, "error": "Unknown report id."}), 400
    try:
        limit = min(max(int(data.get("limit") or 1000), 1), 10_000)
        sql = render_report_sql(report_id, data.get("table") or "")
        with metrics.BQ_QUERY_LATENCY.labels(report_id).time():
            result = bq().query(sql).result(max_results=limit)
    except (ValidationError, ValueError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except NotFound:
        return jsonify({"success": False, "error": "Table not found."}), 404
    columns = [f.name for f in result.schema]
    rows = [list(r.values()) for r in result]
    return jsonify({"success": True, "columns": columns, "rows": rows, "row_count": len(rows)})


@app.route("/download_report")
@require_user
@rate_limit()
def download_report():
    report_id = request.args.get("report")
    table = request.args.get("table", "")
    if report_id not in REPORTS:
        return "Unknown report id.", 400
    try:
        sql = render_report_sql(report_id, table)
        return _csv_response(bq().query(sql).result(), f"{report_id}_{table}.csv")
    except ValidationError as e:
        return str(e), 400
    except NotFound:
        return "Table not found.", 404


# ===============================
# Looker Studio: one BigQuery view per report
# ===============================
def _create_or_replace_view(view_id: str, sql: str):
    try:
        existing = bq().get_table(view_id)
        existing.view_query = sql
        bq().update_table(existing, ["view_query"])
    except NotFound:
        view = bigquery.Table(view_id)
        view.view_query = sql
        bq().create_table(view)


def publish_looker_views_for_table(table_name: str) -> dict:
    """Create/replace views named <table>__<report_id>_v for Looker Studio to connect to."""
    table_name = validate_table_name(table_name)
    created = {}
    for rid in REPORTS:
        view_fq = f"{PROJECT_ID}.{BIGQUERY_DATASET}.{table_name}__{rid}_v"
        _create_or_replace_view(view_fq, render_report_sql(rid, table_name))
        created[rid] = view_fq
    return created


@app.route("/publish_looker_views", methods=["POST"])
@require_user
@rate_limit()
def publish_looker_views():
    table = (request.get_json(silent=True) or {}).get("table", "")
    try:
        return jsonify({"success": True, "views": publish_looker_views_for_table(table)})
    except ValidationError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    except NotFound:
        return jsonify({"success": False, "error": "Table not found."}), 404


@app.route("/looker_help")
@require_user
def looker_help():
    return jsonify({
        "success": True,
        "project_id": PROJECT_ID,
        "dataset": BIGQUERY_DATASET,
        "how_to": [
            "Open https://lookerstudio.google.com -> Create -> Report.",
            "Add data -> BigQuery connector.",
            f"Pick project '{PROJECT_ID}' -> dataset '{BIGQUERY_DATASET}'.",
            "Choose one of the *_v views (e.g. finance_data__dept_totals_v).",
            "Click CONNECT, then add charts.",
        ],
        "tip": "Re-run Publish after uploading a table under a new name.",
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
