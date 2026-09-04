import json
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "demo")
os.environ.setdefault("DB_USER", "demo")
os.environ.setdefault("DB_PASSWORD", "demo")

from monitor import handler  # noqa: E402


def _slot(name, retained_bytes, active=False, slot_type="logical"):
    return {
        "slot_name": name,
        "slot_type": slot_type,
        "active": active,
        "database": "demo",
        "retained_bytes": retained_bytes,
    }


class FakeCursor:
    def __init__(self, rows, columns):
        self._rows = rows
        self.description = [type("Col", (), {"name": c}) for c in columns]

    def execute(self, *_args, **_kwargs):
        pass

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


# --- evaluate_slots: pure logic, no mocking needed ---


def test_evaluate_slots_splits_on_threshold():
    slots = [_slot("a", 100), _slot("b", 500), _slot("c", 1000)]
    healthy, alerting = handler.evaluate_slots(slots, alert_threshold_bytes=500)
    assert [s["slot_name"] for s in healthy] == ["a"]
    assert [s["slot_name"] for s in alerting] == ["b", "c"]


def test_evaluate_slots_all_healthy():
    slots = [_slot("a", 10), _slot("b", 20)]
    healthy, alerting = handler.evaluate_slots(slots, alert_threshold_bytes=1000)
    assert len(healthy) == 2
    assert alerting == []


# --- fetch_slot_status: maps DB rows to dicts ---


def test_fetch_slot_status_maps_columns_to_dicts():
    rows = [("slot_a", "logical", True, "demo", 12345)]
    columns = ["slot_name", "slot_type", "active", "database", "retained_bytes"]
    conn = FakeConnection(FakeCursor(rows, columns))

    result = handler.fetch_slot_status(conn)

    assert result == [
        {
            "slot_name": "slot_a",
            "slot_type": "logical",
            "active": True,
            "database": "demo",
            "retained_bytes": 12345,
        }
    ]


# --- publish_metrics / publish_alert: AWS calls are mocked ---


def test_publish_metrics_sends_one_datapoint_per_slot():
    cloudwatch = MagicMock()
    slots = [_slot("a", 100), _slot("b", 200)]

    handler.publish_metrics(cloudwatch, "pg-slot-sentinel", slots)

    cloudwatch.put_metric_data.assert_called_once()
    call = cloudwatch.put_metric_data.call_args
    assert call.kwargs["Namespace"] == "pg-slot-sentinel"
    assert len(call.kwargs["MetricData"]) == 2
    assert call.kwargs["MetricData"][0]["MetricName"] == "ReplicationSlotRetainedBytes"


def test_publish_metrics_noop_for_empty_slot_list():
    cloudwatch = MagicMock()
    handler.publish_metrics(cloudwatch, "pg-slot-sentinel", [])
    cloudwatch.put_metric_data.assert_not_called()


def test_publish_alert_message_names_each_alerting_slot():
    sns = MagicMock()
    slots = [_slot("orphaned_dms_task_slot", 5 * 1024**3, active=False)]

    message = handler.publish_alert(sns, "arn:aws:sns:us-east-1:123456789012:topic", slots, 1024**3)

    sns.publish.assert_called_once()
    call = sns.publish.call_args
    assert call.kwargs["TopicArn"] == "arn:aws:sns:us-east-1:123456789012:topic"
    assert "orphaned_dms_task_slot" in message
    assert "INACTIVE" in message
    assert message == call.kwargs["Message"]


# --- run(): the orchestration function ---


def test_run_dry_run_never_touches_aws():
    conn = FakeConnection(
        FakeCursor(
            [("orphaned", "logical", False, "demo", 999_999_999)],
            ["slot_name", "slot_type", "active", "database", "retained_bytes"],
        )
    )
    with patch.object(handler, "_connect", return_value=conn), patch("boto3.client") as mock_client:
        result = handler.run(alert_threshold_bytes=1000, dry_run=True)

    mock_client.assert_not_called()
    assert conn.closed
    assert result["dry_run"] is True
    assert result["notified"] is False
    assert [s["slot_name"] for s in result["alerting_slots"]] == ["orphaned"]


def test_run_real_mode_publishes_metrics_and_notifies():
    conn = FakeConnection(
        FakeCursor(
            [("orphaned", "logical", False, "demo", 999_999_999)],
            ["slot_name", "slot_type", "active", "database", "retained_bytes"],
        )
    )
    cloudwatch = MagicMock()
    sns = MagicMock()

    def fake_client(service_name, *_args, **_kwargs):
        return {"cloudwatch": cloudwatch, "sns": sns}[service_name]

    with (
        patch.object(handler, "_connect", return_value=conn),
        patch("boto3.client", side_effect=fake_client),
        patch.dict(os.environ, {"SNS_TOPIC_ARN": "arn:aws:sns:us-east-1:123456789012:topic"}),
    ):
        result = handler.run(alert_threshold_bytes=1000, dry_run=False)

    cloudwatch.put_metric_data.assert_called_once()
    sns.publish.assert_called_once()
    assert result["notified"] is True


def test_run_real_mode_without_topic_arn_skips_notification():
    conn = FakeConnection(
        FakeCursor(
            [("orphaned", "logical", False, "demo", 999_999_999)],
            ["slot_name", "slot_type", "active", "database", "retained_bytes"],
        )
    )
    cloudwatch = MagicMock()
    sns = MagicMock()

    def fake_client(service_name, *_args, **_kwargs):
        return {"cloudwatch": cloudwatch, "sns": sns}[service_name]

    env = dict(os.environ)
    env.pop("SNS_TOPIC_ARN", None)
    with (
        patch.object(handler, "_connect", return_value=conn),
        patch("boto3.client", side_effect=fake_client),
        patch.dict(os.environ, env, clear=True),
    ):
        result = handler.run(alert_threshold_bytes=1000, dry_run=False)

    cloudwatch.put_metric_data.assert_called_once()
    sns.publish.assert_not_called()
    assert result["notified"] is False


# --- _db_password: plain env var vs Secrets Manager ---


def test_db_password_from_plain_env_var():
    with patch.dict(os.environ, {"DB_PASSWORD": "plain-secret"}, clear=False):
        os.environ.pop("DB_PASSWORD_SECRET_ARN", None)
        assert handler._db_password() == "plain-secret"


def test_db_password_from_secrets_manager():
    secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:x"
    secretsmanager = MagicMock()
    secretsmanager.get_secret_value.return_value = {
        "SecretString": json.dumps({"password": "from-secrets-manager"})
    }

    with (
        patch("boto3.client", return_value=secretsmanager),
        patch.dict(os.environ, {"DB_PASSWORD_SECRET_ARN": secret_arn}),
    ):
        assert handler._db_password() == "from-secrets-manager"
    secretsmanager.get_secret_value.assert_called_once_with(SecretId=secret_arn)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
