import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Set before src.api.server is imported: load_dotenv() never overrides an existing
# variable, so a local .env cannot switch tests to a real Ring data source.
os.environ["RING_DATA_SOURCE"] = "simulator"
# Same reason: real Ring or AWS credentials from a local .env never reach the tests.
for _name in ("RING_PARTNER_ACCESS_TOKEN", "RING_PARTNER_DEVICE_ID", "RING_PARTNER_HARDWARE_ID",
              "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
              "RING_PARTNER_HMAC_KEY"):
    os.environ[_name] = ""
# A client built without an explicit base_url must never reach the real Ring API: nothing listens here.
os.environ["RING_PARTNER_BASE_URL"] = "http://127.0.0.1:9"


@pytest.fixture(autouse=True)
def no_real_aws_clients(monkeypatch):
    """Any boto3 client creation (and so any Bedrock call) fails unless a test replaces it."""
    def refuse(*_args, **_kwargs):
        raise AssertionError("Tests must not create real AWS clients; mock _call_llm or boto3.client.")
    monkeypatch.setattr("boto3.client", refuse)
