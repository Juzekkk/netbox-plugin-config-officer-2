import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest import mock

from config_officer import webhook


def test_signature_is_hmac_sha256_of_the_body():
    body = b'{"a": 1}'
    expected = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert webhook.sign(body, "secret") == expected


def test_payload_and_delivery():
    result = SimpleNamespace(
        device_name="sw1", status="succeeded", failed_reason="", message="ok", config_changed=False
    )
    failed = SimpleNamespace(
        device_name="sw2",
        status="failed",
        failed_reason="fail-connect",
        message="x",
        config_changed=None,
    )
    summary = SimpleNamespace(
        target="tags: core",
        label="schedule_nightly_2026",
        results=[result, failed],
        commit=None,
        counts={"total": 2, "succeeded": 1, "failed": 1, "skipped": 0, "changed": 0},
        started=datetime(2026, 11, 3, tzinfo=UTC),
        finished=None,
    )
    payload = webhook.build_payload("nightly", summary)
    assert payload["event"] == "schedule_complete"
    assert payload["summary"]["failed"] == 1
    assert [d["device"] for d in payload["diffs"]] == ["sw1"]  # failed devices have no diff entry

    with mock.patch("config_officer.webhook.requests.post") as post:
        post.return_value.status_code = 200
        assert "delivered" in webhook.send("http://hook", "s3cret", payload)
    sent_body = post.call_args.kwargs["data"]
    assert json.loads(sent_body) == payload
    assert post.call_args.kwargs["headers"]["X-Webhook-Signature"] == webhook.sign(
        sent_body, "s3cret"
    )
