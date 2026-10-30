import io
from unittest.mock import MagicMock

import pytest
from prometheus_client import REGISTRY

from conftest import webapp

IAP = {"X-Goog-Authenticated-User-Email": "accounts.google.com:carol@example.com"}


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    monkeypatch.setattr(webapp, "gcs", lambda: MagicMock())
    return webapp.app.test_client()


def upload(client, name, data):
    return client.post("/", data={"file": (io.BytesIO(data), name)},
                       headers=IAP, content_type="multipart/form-data")


def test_metrics_endpoint_is_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.content_type.startswith("text/plain")
    assert b"webapp_uploads_total" in r.data


def test_upload_outcomes_are_counted(client):
    accepted = sample("webapp_uploads_total", format="csv", outcome="accepted")
    rejected = sample("webapp_uploads_total", format="parquet", outcome="rejected_validation")
    other = sample("webapp_uploads_total", format="other", outcome="rejected_validation")

    upload(client, "ok.csv", b"a,b\n1,2\n")
    upload(client, "fake.parquet", b"a,b\n1,2\n")
    upload(client, "dump.sql", b"select 1")

    assert sample("webapp_uploads_total", format="csv", outcome="accepted") == accepted + 1
    assert sample("webapp_uploads_total", format="parquet", outcome="rejected_validation") == rejected + 1
    assert sample("webapp_uploads_total", format="other", outcome="rejected_validation") == other + 1


def test_rate_limited_requests_are_counted(client):
    before = sample("webapp_rate_limited_total", endpoint="/")
    for _ in range(4):  # RATE_LIMIT_REQUESTS=3 in conftest
        upload(client, "t.csv", b"a,b\n1,2\n")
    assert sample("webapp_rate_limited_total", endpoint="/") == before + 1


def test_endpoint_label_uses_route_template_not_raw_path(client, monkeypatch):
    bq = MagicMock()
    bq.get_table.return_value = MagicMock(num_rows=1)
    monkeypatch.setattr(webapp, "bq", lambda: bq)
    client.get("/status/some_table_name", headers=IAP)
    assert sample("webapp_http_requests_total", method="GET", endpoint="/status/<table>", status="200") >= 1
    assert sample("webapp_http_requests_total", method="GET", endpoint="/status/some_table_name", status="200") == 0


def test_storage_failure_is_counted_and_returns_502(client, monkeypatch):
    broken = MagicMock()
    broken.bucket.return_value.blob.return_value.upload_from_file.side_effect = ConnectionError("GCS down")
    monkeypatch.setattr(webapp, "gcs", lambda: broken)
    before = sample("webapp_uploads_total", format="csv", outcome="error")
    r = upload(client, "ok.csv", b"a,b\n1,2\n")
    assert r.status_code == 502
    assert sample("webapp_uploads_total", format="csv", outcome="error") == before + 1
