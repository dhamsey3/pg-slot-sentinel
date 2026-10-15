import json
import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("DB_HOST", "localhost")
os.environ.setdefault("DB_NAME", "demo")
os.environ.setdefault("DB_USER", "demo")
os.environ.setdefault("DB_PASSWORD", "demo")

from monitor import handler  # noqa: E402

SLOT_COLUMNS = ["slot_name", "slot_type", "active", "database", "retained_bytes"]
TOPIC_ARN = "arn:aws:sns:us-east-1:123456789012:topic"


def make_slot(name, retained_bytes, active=False, slot_type="logical"):
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


def orphaned_slot_connection():
    """A database with one inactive logical slot retaining ~1 GB of WAL."""
    rows = [("orphaned", "logical", False, "demo", 999_999_999)]
    return FakeConnection(FakeCursor(rows, SLOT_COLUMNS))


def fake_boto3_client(**clients):
    """A boto3.client stand-in that hands out the given mocks by service name."""
    return lambda service_name, *_args, **_kwargs: clients[service_name]


# --- split_by_threshold: pure logic, no mocking needed ---


def test_split_by_threshold_alerts_at_and_above_threshold():
    slots = [make_slot("a", 100), make_slot("b", 500), make_slot("c", 1000)]
    healthy, alerting = handler.split_by_threshold(slots, threshold_bytes=500)
    assert [s["slot_name"] for s in healthy] == ["a"]
    assert [s["slot_name"] for s in alerting] == ["b", "c"]


def test_split_by_threshold_all_healthy():
    slots = [make_slot("a", 10), make_slot("b", 20)]
    healthy, alerting = handler.split_by_threshold(slots, threshold_bytes=1000)
    assert len(healthy) == 2
    assert alerting == []


def test_split_by_threshold_slot_retaining_nothing_is_healthy():
    # What the query reports for a slot whose restart_lsn is NULL.
    slots = [make_slot("standby_not_yet_connected", 0, slot_type="physical")]
    healthy, alerting = handler.split_by_threshold(slots, threshold_bytes=1)
    assert [s["slot_name"] for s in healthy] == ["standby_not_yet_connected"]
    assert alerting == []


# --- fetch_replication_slots: maps DB rows to dicts ---


def test_fetch_replication_slots_maps_columns_to_dicts():
    conn = FakeConnection(FakeCursor([("slot_a", "logical", True, "demo", 12345)], SLOT_COLUMNS))

    result = handler.fetch_replication_slots(conn)

    assert result == [
        {
            "slot_name": "slot_a",
            "slot_type": "logical",
            "active": True,
            "database": "demo",
            "retained_bytes": 12345,
        }
    ]


# --- publish_retention_metrics / send_retention_alert: AWS calls are mocked ---


def test_publish_retention_metrics_sends_one_datapoint_per_slot():
    cloudwatch = MagicMock()
    slots = [make_slot("a", 100), make_slot("b", 200)]

    handler.publish_retention_metrics(cloudwatch, "pg-slot-sentinel", slots)

    cloudwatch.put_metric_data.assert_called_once()
    call = cloudwatch.put_metric_data.call_args
    assert call.kwargs["Namespace"] == "pg-slot-sentinel"
    assert len(call.kwargs["MetricData"]) == 2
    assert call.kwargs["MetricData"][0]["MetricName"] == "ReplicationSlotRetainedBytes"


def test_publish_retention_metrics_noop_for_empty_slot_list():
    cloudwatch = MagicMock()
    handler.publish_retention_metrics(cloudwatch, "pg-slot-sentinel", [])
    cloudwatch.put_metric_data.assert_not_called()


def test_send_retention_alert_names_each_alerting_slot():
    sns = MagicMock()
    slots = [make_slot("orphaned_dms_task_slot", 5 * handler.GIB, active=False)]

    message = handler.send_retention_alert(sns, TOPIC_ARN, slots, handler.GIB)

    sns.publish.assert_called_once()
    call = sns.publish.call_args
    assert call.kwargs["TopicArn"] == TOPIC_ARN
    assert "orphaned_dms_task_slot" in message
    assert "INACTIVE" in message
    assert message == call.kwargs["Message"]


# --- check_replication_slots: the orchestration function ---


def test_check_dry_run_never_touches_aws():
    conn = orphaned_slot_connection()
    with (
        patch.object(handler, "_connect_to_database", return_value=conn),
        patch("boto3.client") as mock_client,
    ):
        result = handler.check_replication_slots(threshold_bytes=1000, dry_run=True)

    mock_client.assert_not_called()
    assert conn.closed
    assert result["dry_run"] is True
    assert result["notified"] is False
    assert [s["slot_name"] for s in result["alerting_slots"]] == ["orphaned"]


def test_check_records_when_it_ran():
    with patch.object(handler, "_connect_to_database", return_value=orphaned_slot_connection()):
        result = handler.check_replication_slots(threshold_bytes=1000, dry_run=True)

    checked_at = datetime.fromisoformat(result["checked_at"])
    assert checked_at.tzinfo == timezone.utc
    assert abs((datetime.now(timezone.utc) - checked_at).total_seconds()) < 60


def test_check_reads_threshold_from_env_when_not_given():
    with (
        patch.object(handler, "_connect_to_database", return_value=orphaned_slot_connection()),
        patch.dict(os.environ, {"WAL_RETENTION_ALERT_BYTES": str(2 * handler.GIB)}),
    ):
        result = handler.check_replication_slots(dry_run=True)

    assert result["threshold_bytes"] == 2 * handler.GIB
    assert [s["slot_name"] for s in result["healthy_slots"]] == ["orphaned"]
    assert result["alerting_slots"] == []


def test_check_publishes_metrics_and_notifies():
    cloudwatch = MagicMock()
    sns = MagicMock()

    with (
        patch.object(handler, "_connect_to_database", return_value=orphaned_slot_connection()),
        patch("boto3.client", side_effect=fake_boto3_client(cloudwatch=cloudwatch, sns=sns)),
        patch.dict(os.environ, {"SNS_TOPIC_ARN": TOPIC_ARN}),
    ):
        result = handler.check_replication_slots(threshold_bytes=1000, dry_run=False)

    cloudwatch.put_metric_data.assert_called_once()
    sns.publish.assert_called_once()
    assert result["notified"] is True


def test_check_without_topic_arn_skips_notification():
    cloudwatch = MagicMock()
    sns = MagicMock()

    env = dict(os.environ)
    env.pop("SNS_TOPIC_ARN", None)
    with (
        patch.object(handler, "_connect_to_database", return_value=orphaned_slot_connection()),
        patch("boto3.client", side_effect=fake_boto3_client(cloudwatch=cloudwatch, sns=sns)),
        patch.dict(os.environ, env, clear=True),
    ):
        result = handler.check_replication_slots(threshold_bytes=1000, dry_run=False)

    cloudwatch.put_metric_data.assert_called_once()
    sns.publish.assert_not_called()
    assert result["notified"] is False


# --- _read_db_password: plain env var vs Secrets Manager ---


def test_read_db_password_from_plain_env_var():
    with patch.dict(os.environ, {"DB_PASSWORD": "plain-secret"}, clear=False):
        os.environ.pop("DB_PASSWORD_SECRET_ARN", None)
        assert handler._read_db_password() == "plain-secret"


def test_read_db_password_from_secrets_manager():
    secret_arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:x"
    secretsmanager = MagicMock()
    secretsmanager.get_secret_value.return_value = {
        "SecretString": json.dumps({"password": "from-secrets-manager"})
    }

    with (
        patch("boto3.client", return_value=secretsmanager),
        patch.dict(os.environ, {"DB_PASSWORD_SECRET_ARN": secret_arn}),
    ):
        assert handler._read_db_password() == "from-secrets-manager"
    secretsmanager.get_secret_value.assert_called_once_with(SecretId=secret_arn)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
