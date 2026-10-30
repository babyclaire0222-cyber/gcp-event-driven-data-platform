"""webapp/main.py and functions/ingest/main.py are both called `main` (the name
Cloud Run / Cloud Functions expect), so load them under distinct module names."""
import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "webapp"))  # for `import validation`, `import ratelimit`

os.environ.setdefault("GCP_PROJECT", "test-project")
os.environ.setdefault("BUCKET_NAME", "test-bucket")
os.environ.setdefault("RATE_LIMIT_REQUESTS", "3")
os.environ.setdefault("RATE_LIMIT_WINDOW", "60")
os.environ.setdefault("MAX_UPLOAD_MB", "1")
os.environ.pop("REDIS_URL", None)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


webapp = _load("webapp_main", ROOT / "webapp" / "main.py")
ingest = _load("ingest_main", ROOT / "functions" / "ingest" / "main.py")


@pytest.fixture(autouse=True)
def reset_rate_limits():
    import ratelimit

    ratelimit.backend = ratelimit.MemoryBackend()
    yield
