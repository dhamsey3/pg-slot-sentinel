#!/usr/bin/env python3
"""Reproduces the failure mode this project exists to catch, end to end:

1. Creates `orphaned_dms_task_slot` and generates real WAL traffic against
   a demo table - simulating a consumer (think a DMS replication task)
   that existed while the traffic happened, then vanished without its
   slot ever being dropped.
2. Only then creates `healthy_consumer_slot`, representing a consumer in
   good standing right now - a freshly (re)connected slot naturally starts
   at the current WAL position, exactly like a consumer that is actually
   keeping up. Alongside it, `standby_not_yet_connected` is a physical slot
   reserved for a standby that has not connected yet: it pins no WAL, and
   Postgres reports its restart_lsn as NULL.
3. Runs the actual monitor (the same monitor.handler.check_replication_slots()
   the Lambda uses) and shows it correctly flags only the orphaned slot.
4. Drops the orphaned slot - the real fix - and re-runs the monitor to
   confirm the retained WAL is gone.

Run from the demo/ directory with the docker-compose Postgres up:
    docker compose up -d
    pip install -r requirements.txt
    python run_demo.py
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "55432")
os.environ.setdefault("DB_NAME", "demo")
os.environ.setdefault("DB_USER", "demo")
os.environ.setdefault("DB_PASSWORD", "demo")
os.environ.setdefault("WAL_RETENTION_ALERT_BYTES", str(100 * 1024))  # 100 KiB, demo-sized

import psycopg2  # noqa: E402

from monitor import handler  # noqa: E402

ORPHANED_SLOT = "orphaned_dms_task_slot"
HEALTHY_SLOT = "healthy_consumer_slot"
UNUSED_STANDBY_SLOT = "standby_not_yet_connected"

CONNECT_ATTEMPTS = 30


def connect_with_retry():
    """Connects to the demo database, waiting up to ~30 s for it to start."""
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            return psycopg2.connect(
                host=os.environ["DB_HOST"],
                port=os.environ["DB_PORT"],
                dbname=os.environ["DB_NAME"],
                user=os.environ["DB_USER"],
                password=os.environ["DB_PASSWORD"],
            )
        except psycopg2.OperationalError:
            if attempt == CONNECT_ATTEMPTS:
                raise
            time.sleep(1)


def enable_autocommit(conn):
    conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
    return conn


def print_section(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def slot_exists(cur, name):
    cur.execute("SELECT 1 FROM pg_replication_slots WHERE slot_name = %s", (name,))
    return cur.fetchone() is not None


def ensure_logical_slot(cur, name):
    if not slot_exists(cur, name):
        cur.execute("SELECT pg_create_logical_replication_slot(%s, 'test_decoding')", (name,))


def ensure_physical_slot(cur, name):
    # immediately_reserve defaults to false: the slot holds no WAL until a
    # standby first connects, so its restart_lsn stays NULL.
    if not slot_exists(cur, name):
        cur.execute("SELECT pg_create_physical_replication_slot(%s)", (name,))


def main():
    conn = enable_autocommit(connect_with_retry())
    cur = conn.cursor()

    print_section("1. A consumer's slot exists while traffic happens, then the consumer vanishes")
    cur.execute("DROP TABLE IF EXISTS demo_events")
    cur.execute(
        "CREATE TABLE demo_events ("
        "id serial PRIMARY KEY, payload text, created_at timestamptz DEFAULT now())"
    )
    # Autovacuum on a table this write-heavy would generate its own WAL in
    # the background and blur the comparison between the two slots. Disabled
    # here purely so the demo's numbers are attributable to the scenario
    # being demonstrated, not background maintenance activity.
    cur.execute("ALTER TABLE demo_events SET (autovacuum_enabled = false)")
    ensure_logical_slot(cur, ORPHANED_SLOT)
    print(f"Created {ORPHANED_SLOT} - this stands in for a DMS task that is about to be stopped.")

    for i in range(800):
        cur.execute(
            "INSERT INTO demo_events (payload) VALUES (%s)",
            (f"event-{i}-" + "x" * 2000,),
        )
    print(f"Wrote 800 rows while {ORPHANED_SLOT} existed. Its consumer is gone; the slot was not.")

    print_section("2. A healthy consumer connects right now, starting from the current position")
    ensure_logical_slot(cur, HEALTHY_SLOT)
    print(f"Created {HEALTHY_SLOT} - a consumer in good standing right now: caught up.")
    ensure_physical_slot(cur, UNUSED_STANDBY_SLOT)
    print(f"Created {UNUSED_STANDBY_SLOT} - reserved for a standby, pinning no WAL yet.")

    print_section("3. Running the monitor (dry-run, same code path as the Lambda)")
    result = handler.check_replication_slots(dry_run=True)
    print(json.dumps(result, indent=2, default=str))

    alerting_names = {s["slot_name"] for s in result["alerting_slots"]}
    healthy_names = {s["slot_name"] for s in result["healthy_slots"]}
    assert ORPHANED_SLOT in alerting_names, "expected the orphaned slot to be flagged"
    assert HEALTHY_SLOT in healthy_names, "the healthy slot should NOT be flagged"
    assert UNUSED_STANDBY_SLOT in healthy_names, "a slot pinning no WAL should NOT be flagged"
    print(
        f"\nCorrectly flagged '{ORPHANED_SLOT}' and did NOT flag "
        f"'{HEALTHY_SLOT}' or '{UNUSED_STANDBY_SLOT}'."
    )

    print_section(f"4. Applying the fix: dropping '{ORPHANED_SLOT}'")
    cur.execute("SELECT pg_drop_replication_slot(%s)", (ORPHANED_SLOT,))
    print(f"Dropped {ORPHANED_SLOT}.")

    print_section("5. Re-running the monitor to confirm the retained WAL is gone")
    result = handler.check_replication_slots(dry_run=True)
    print(json.dumps(result, indent=2, default=str))
    assert not result["alerting_slots"], "expected no alerting slots after the fix"
    print("\nNo alerting slots remain. That is the real remediation:")
    print("stop the dead consumer, drop its slot, WAL is reclaimed.")

    for name in (HEALTHY_SLOT, UNUSED_STANDBY_SLOT):
        cur.execute("SELECT pg_drop_replication_slot(%s)", (name,))
    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
