"""Detects PostgreSQL logical/physical replication slots that are quietly
retaining WAL because nothing is consuming them anymore - the classic
"a consumer was torn down but its replication slot was not dropped"
failure mode. Left unnoticed, retained WAL accumulates until it fills the
instance's storage.

Runs equally well as a Lambda handler (lambda_handler) or a local script
(python -m monitor.handler) against the demo database.
"""

import json
import logging
import os
from datetime import datetime, timezone

import boto3
import psycopg2

logger = logging.getLogger()
logger.setLevel(logging.INFO)

GIB = 1024**3
DEFAULT_THRESHOLD_BYTES = 5 * GIB
DEFAULT_METRIC_NAMESPACE = "pg-slot-sentinel"

# restart_lsn is NULL for a slot that pins no WAL at all: a physical slot
# created without reserving WAL (a standby that has not connected yet) or a
# slot Postgres has invalidated. pg_wal_lsn_diff() then returns NULL too, so
# count those as retaining 0 bytes rather than letting a NULL break the
# comparison for every other slot. The bigint cast keeps retained_bytes a
# plain int instead of numeric/Decimal.
REPLICATION_SLOTS_QUERY = """
    SELECT
        slot_name,
        slot_type,
        active,
        COALESCE(database, '') AS database,
        COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)::bigint
            AS retained_bytes
    FROM pg_replication_slots
"""


def _env_flag(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes"}


def _read_db_password():
    secret_arn = os.environ.get("DB_PASSWORD_SECRET_ARN")
    if secret_arn:
        client = boto3.client("secretsmanager")
        secret = client.get_secret_value(SecretId=secret_arn)
        payload = json.loads(secret["SecretString"])
        return payload["password"]
    return os.environ["DB_PASSWORD"]


def _connect_to_database():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=_read_db_password(),
        connect_timeout=5,
    )


def fetch_replication_slots(conn):
    """Returns one dict per replication slot, including bytes of WAL it is
    forcing the server to retain."""
    with conn.cursor() as cur:
        cur.execute(REPLICATION_SLOTS_QUERY)
        columns = [c.name for c in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]


def split_by_threshold(slots, threshold_bytes):
    """Splits slots into healthy vs. alerting, using the same signal a
    human would: how much WAL is this slot forcing Postgres to hold onto,
    regardless of whether the slot happens to be "active" right now (a
    slot can sit idle between legitimate consumer runs without being a
    problem - retained bytes crossing the threshold is what matters)."""
    healthy, alerting = [], []
    for slot in slots:
        (alerting if slot["retained_bytes"] >= threshold_bytes else healthy).append(slot)
    return healthy, alerting


def publish_retention_metrics(cloudwatch, namespace, slots):
    if not slots:
        return
    cloudwatch.put_metric_data(
        Namespace=namespace,
        MetricData=[
            {
                "MetricName": "ReplicationSlotRetainedBytes",
                "Dimensions": [{"Name": "SlotName", "Value": slot["slot_name"]}],
                "Value": float(slot["retained_bytes"]),
                "Unit": "Bytes",
            }
            for slot in slots
        ],
    )


def send_retention_alert(sns, topic_arn, alerting_slots, threshold_bytes):
    lines = [
        f"{len(alerting_slots)} replication slot(s) retaining "
        f">= {threshold_bytes / GIB:.1f} GiB of WAL:",
        "",
    ]
    for slot in alerting_slots:
        status = "active" if slot["active"] else "INACTIVE"
        lines.append(
            f"  - {slot['slot_name']} ({slot['slot_type']}, {status}): "
            f"{slot['retained_bytes'] / GIB:.2f} GiB retained"
        )
    lines.append("")
    lines.append(
        "An inactive slot retaining a large amount of WAL usually means its "
        "consumer (e.g. a DMS task) was stopped or torn down without "
        "dropping the slot. Confirm the consumer is really gone, then "
        "drop the slot with SELECT pg_drop_replication_slot('<name>')."
    )
    message = "\n".join(lines)
    sns.publish(
        TopicArn=topic_arn,
        Subject="pg-slot-sentinel: replication slot(s) retaining excess WAL",
        Message=message,
    )
    return message


def check_replication_slots(threshold_bytes=None, dry_run=None):
    if threshold_bytes is None:
        threshold_bytes = int(os.environ.get("WAL_RETENTION_ALERT_BYTES", DEFAULT_THRESHOLD_BYTES))
    if dry_run is None:
        dry_run = _env_flag("DRY_RUN", default=True)

    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _connect_to_database()
    try:
        slots = fetch_replication_slots(conn)
    finally:
        conn.close()

    healthy, alerting = split_by_threshold(slots, threshold_bytes)

    result = {
        "checked_at": checked_at,
        "threshold_bytes": threshold_bytes,
        "slot_count": len(slots),
        "healthy_slots": healthy,
        "alerting_slots": alerting,
        "dry_run": dry_run,
        "notified": False,
    }

    if dry_run:
        for slot in alerting:
            logger.warning(
                "[DRY RUN] slot %s retaining %.2f GiB (>= threshold) - "
                "would publish metric and alert",
                slot["slot_name"],
                slot["retained_bytes"] / GIB,
            )
        return result

    namespace = os.environ.get("METRIC_NAMESPACE", DEFAULT_METRIC_NAMESPACE)
    publish_retention_metrics(boto3.client("cloudwatch"), namespace, slots)

    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if alerting and topic_arn:
        send_retention_alert(boto3.client("sns"), topic_arn, alerting, threshold_bytes)
        result["notified"] = True
    elif alerting:
        logger.warning(
            "%d slot(s) alerting but SNS_TOPIC_ARN is not set - no alert sent", len(alerting)
        )

    return result


def lambda_handler(event, context):
    result = check_replication_slots()
    logger.info("pg-slot-sentinel result: %s", json.dumps(result, default=str))
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(json.dumps(check_replication_slots(), indent=2, default=str))
