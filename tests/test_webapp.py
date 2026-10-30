import io
from unittest.mock import MagicMock

import pytest
from google.api_core.exceptions import NotFound

from conftest import webapp

IAP = {"X-Goog-Authenticated-User-Email": "accounts.google.com:alice@example.com"}
BOB = {"X-Goog-Authenticated-User-Email": "accounts.google.com:bob@example.com"}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("DEV_USER_EMAIL", raising=False)
    monkeypatch.delenv("K_SERVICE", raising=False)
    return webapp.app.test_client()


@pytest.fixture
def gcs(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(webapp, "gcs", lambda: client)
    return client


@pytest.fixture
def bq(monkeypatch):
    client = MagicMock()
    monkeypatch.setattr(webapp, "bq", lambda: client)
    return client


def upload(client, name, data, headers=IAP):
    return client.post("/", data={"file": (io.BytesIO(data), name)},
                       headers=headers, content_type="multipart/form-data")


def test_requires_iap_identity(client):
    assert client.get("/whoami").status_code == 401
    r = client.get("/whoami", headers=IAP)
    assert r.status_code == 200 and r.json["email"] == "alice@example.com"


def test_dev_user_override_is_ignored_on_cloud_run(client, monkeypatch):
    monkeypatch.setenv("DEV_USER_EMAIL", "dev@example.com")
    assert client.get("/whoami").status_code == 200
    monkeypatch.setenv("K_SERVICE", "webapp")  # set by Cloud Run
    assert client.get("/whoami").status_code == 401


def test_valid_csv_goes_to_gcs_uploads_prefix(client, gcs):
    r = upload(client, "Sales Q1.csv", b"department,amount\nIT,10\n")
    assert r.status_code == 202
    assert r.json["table"] == "sales_q1" and r.json["status"] == "processing"
    gcs.bucket.assert_called_with("test-bucket")
    gcs.bucket().blob.assert_called_with("uploads/sales_q1.csv")
    gcs.bucket().blob().upload_from_file.assert_called_once()


@pytest.mark.parametrize("name,data,msg", [
    ("dump.sql", b"CREATE TABLE x (a int);", "Unsupported"),
    ("fake.parquet", b"a,b\n1,2\n", "Parquet"),
    ("list.json", b'[{"a": 1},\n{"a": 2}]', "newline-delimited"),
    ("one.csv", b"onlycol\n1\n", "header"),
])
def test_bad_files_rejected_before_gcs(client, gcs, name, data, msg):
    r = upload(client, name, data)
    assert r.status_code == 400 and msg in r.json["error"]
    gcs.bucket().blob().upload_from_file.assert_not_called()


def test_oversized_upload_returns_413(client, gcs):
    r = upload(client, "big.csv", b"a,b\n" + b"1,2\n" * 300_000)  # > MAX_UPLOAD_MB=1
    assert r.status_code == 413


def test_upload_is_rate_limited_per_user(client, gcs):
    for _ in range(3):
        assert upload(client, "t.csv", b"a,b\n1,2\n").status_code == 202
    r = upload(client, "t.csv", b"a,b\n1,2\n")
    assert r.status_code == 429 and "Retry-After" in r.headers
    assert upload(client, "t.csv", b"a,b\n1,2\n", headers=BOB).status_code == 202


def test_spoofed_forwarded_for_does_not_reset_limit(client, gcs):
    for i in range(3):
        upload(client, "t.csv", b"a,b\n1,2\n", headers={**IAP, "X-Forwarded-For": f"10.0.0.{i}"})
    r = upload(client, "t.csv", b"a,b\n1,2\n", headers={**IAP, "X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429


def test_status_reports_loaded_rejected_processing(client, gcs, bq):
    bq.get_table.return_value = MagicMock(num_rows=42)
    r = client.get("/status/sales", headers=IAP)
    assert r.json == {"success": True, "status": "loaded", "rows": 42}

    bq.get_table.side_effect = NotFound("nope")
    gcs.list_blobs.return_value = iter([MagicMock(metadata={"rejection-reason": "bad row 3"})])
    assert client.get("/status/sales", headers=IAP).json["reason"] == "bad row 3"

    gcs.list_blobs.return_value = iter([])
    assert client.get("/status/sales", headers=IAP).json["status"] == "processing"


def test_table_params_are_validated_before_sql(client, bq):
    r = client.get("/download_bq", query_string={"table": "x`; DROP TABLE y; --"}, headers=IAP)
    assert r.status_code == 400
    r = client.post("/run_report", json={"report": "dept_totals", "table": "a b"}, headers=IAP)
    assert r.status_code == 400
    bq.query.assert_not_called()
    bq.list_rows.assert_not_called()


def test_report_sql_uses_detected_columns(bq):
    fields = [MagicMock(), MagicMock(), MagicMock()]
    for f, n in zip(fields, ["Dept", "Spend", "txn_date"], strict=False):
        f.name = n
    bq.get_table.return_value = MagicMock(schema=fields)
    sql = webapp.render_report_sql("dept_totals", "finance")
    assert "`Dept` AS department" in sql and "SUM(`Spend`)" in sql
    assert "`test-project.analysis_dataset.finance`" in sql


def test_excel_is_converted_to_csv_before_gcs(client, gcs):
    import pandas as pd

    buf = io.BytesIO()
    pd.DataFrame({"department": ["IT"], "amount": [10]}).to_excel(buf, index=False)
    r = upload(client, "budget.xlsx", buf.getvalue())
    assert r.status_code == 202
    gcs.bucket().blob.assert_called_with("uploads/budget.csv")
    data = gcs.bucket().blob().upload_from_string.call_args.args[0]
    assert data.decode().splitlines() == ["department,amount", "IT,10"]


def test_txt_is_converted_to_csv_before_gcs(client, gcs):
    r = upload(client, "budget.txt", b"department\tamount\nIT\t10\n")
    assert r.status_code == 202
    gcs.bucket().blob.assert_called_with("uploads/budget.csv")
    data = gcs.bucket().blob().upload_from_string.call_args.args[0]
    assert data.decode().split() == ["department,amount", "IT,10"]


def test_free_form_txt_is_rejected_before_gcs(client, gcs):
    r = upload(client, "notes.txt", b"Just some meeting notes.\nNothing tabular here.\n")
    assert r.status_code == 400 and "must be a table" in r.json["error"]
    gcs.bucket().blob().upload_from_string.assert_not_called()
