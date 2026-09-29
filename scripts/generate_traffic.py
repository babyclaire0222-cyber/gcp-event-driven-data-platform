"""Send a realistic mix of traffic to a local webapp so the Grafana dashboard has data.

    python scripts/generate_traffic.py --minutes 5

Mix: health checks, status polls, valid uploads from sample_data/, and deliberately
bad uploads (wrong format, renamed files) so rejection and rate-limit panels move.
Valid uploads go to your real dev bucket, so keep --minutes small.
Local only: relies on DEV_USER_EMAIL, since there is no IAP in front of compose.
"""
import argparse
import random
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

SAMPLE = Path(__file__).resolve().parent.parent / "sample_data" / "finance_6months.csv"
BAD_FILES = [
    ("dump.sql", b"CREATE TABLE t (a INT);"),
    ("renamed.parquet", b"a,b\n1,2\n"),
    ("array.json", b'[{"a": 1},\n{"a": 2}]'),
    ("onecol.csv", b"only\n1\n"),
]


def request(base, method, path, body=None, headers=None):
    req = urllib.request.Request(base + path, data=body, method=method, headers=headers or {})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (scheme checked in main)
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def upload(base, name, data):
    boundary = uuid.uuid4().hex
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
        "Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    return request(base, "POST", "/", body, {"Content-Type": f"multipart/form-data; boundary={boundary}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8080")
    ap.add_argument("--minutes", type=float, default=3)
    ap.add_argument("--good-ratio", type=float, default=0.6, help="share of uploads that are valid")
    args = ap.parse_args()
    if not args.url.startswith(("http://", "https://")):
        ap.error("--url must be http(s)")

    good = SAMPLE.read_bytes()
    end = time.time() + args.minutes * 60
    counts = {}
    while time.time() < end:
        roll = random.random()  # noqa: S311 (not crypto)
        if roll < 0.3:
            status = request(args.url, "GET", "/healthz")
        elif roll < 0.6:
            status = request(args.url, "GET", "/status/finance_6months")
        elif random.random() < args.good_ratio:  # noqa: S311
            status = upload(args.url, "finance_6months.csv", good)
        else:
            status = upload(args.url, *random.choice(BAD_FILES))  # noqa: S311
        counts[status] = counts.get(status, 0) + 1
        time.sleep(random.uniform(0.5, 2))  # noqa: S311
    print("responses by status:", dict(sorted(counts.items())))


if __name__ == "__main__":
    main()
