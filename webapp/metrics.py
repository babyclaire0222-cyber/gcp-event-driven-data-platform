"""Prometheus metrics for the webapp, served at /metrics.

Labels are kept low-cardinality on purpose: endpoints use the route template
(`/status/<table>`, not `/status/sales`) and outcomes are a fixed set, so a
user uploading 10,000 differently named files can't blow up the time series.

gunicorn runs a single process (see Dockerfile), so the default in-process
registry is accurate without prometheus_client's multiprocess mode.
"""
import time

from flask import Flask, Response, g, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

HTTP_REQUESTS = Counter(
    "webapp_http_requests_total", "HTTP requests", ["method", "endpoint", "status"]
)
HTTP_LATENCY = Histogram(
    "webapp_http_request_duration_seconds", "HTTP request latency", ["endpoint"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
UPLOADS = Counter(
    "webapp_uploads_total", "Upload attempts by format and outcome",
    ["format", "outcome"],  # outcome: accepted | rejected_validation | rejected_too_large | error
)
UPLOAD_BYTES = Histogram(
    "webapp_upload_size_bytes", "Size of accepted uploads",
    buckets=(1e3, 1e4, 1e5, 1e6, 5e6, 1e7, 5e7),
)
RATE_LIMITED = Counter("webapp_rate_limited_total", "Requests rejected with 429", ["endpoint"])
BQ_QUERY_LATENCY = Histogram(
    "webapp_bigquery_query_duration_seconds", "BigQuery report query latency", ["report"],
    buckets=(0.25, 0.5, 1, 2, 4, 8, 16, 32),
)


def _endpoint() -> str:
    return request.url_rule.rule if request.url_rule else "unmatched"


def init_app(app: Flask) -> None:
    @app.before_request
    def _start_timer():
        g._metrics_start = time.perf_counter()

    @app.after_request
    def _record(response):
        endpoint = _endpoint()
        if endpoint != "/metrics":
            HTTP_REQUESTS.labels(request.method, endpoint, str(response.status_code)).inc()
            start = getattr(g, "_metrics_start", None)
            if start is not None:
                HTTP_LATENCY.labels(endpoint).observe(time.perf_counter() - start)
        return response

    @app.route("/metrics")
    def metrics():
        # Not behind require_user: on Cloud Run all traffic already passes IAP,
        # and locally Prometheus scrapes it over the compose network.
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)
