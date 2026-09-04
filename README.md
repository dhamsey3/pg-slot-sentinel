# pg-slot-sentinel

A small monitor that catches a specific, nasty PostgreSQL/RDS failure mode
before it becomes an emergency: a logical replication slot left behind by a
consumer that no longer exists, quietly forcing the database to retain WAL
until the volume fills up.

The scenario below walks through how this failure mode plays out in
practice, and how it gets fixed - it's the real, well-documented way this
happens, not a hypothetical.

## The failure mode

**Symptom.** Free storage on an RDS Postgres instance had been declining
for about a week, at a rate that didn't line up with normal traffic growth.

**The red herring.** A large one-off backfill job had been run against the
same database a few days earlier, migrating a legacy table to a new schema.
That was the obvious first suspect — it had touched millions of rows, it
was recent, and "the backfill left something bloated behind" is a
completely reasonable hypothesis. A day went into checking table and index
bloat, confirming autovacuum was keeping up, and diffing table sizes
before and after the backfill. None of it explained the gap.

**Root cause.** Two DMS replication tasks, set up months earlier to feed a
downstream reporting pipeline, had been stopped after that pipeline was
decommissioned. Stopping a DMS task does not drop the logical replication
slot it created on the source database. Postgres has no way to know the
consumer is gone — from its point of view, the slot might resume reading
at any moment, so it keeps every WAL segment since the slot's last
confirmed position, indefinitely. The two slots had been sitting inactive
for months, quietly forcing the server to retain roughly 1.8 TB of WAL
that nothing would ever read.

**Fix.** Confirm the DMS tasks were genuinely gone and not just paused,
then drop the corresponding replication slots (`SELECT
pg_drop_replication_slot('slot_name')`). That freed the retained WAL
immediately — Postgres could finally recycle segments it had been forced
to hold onto for months.

**The gap.** Nothing was watching for this. A slot's retained-WAL size is
visible in `pg_replication_slots` and in RDS's own `OldestReplicationSlotLag`
/ `TransactionLogsDiskUsage` CloudWatch metrics, but nobody had wired an
alert to either — an inactive slot from a decommissioned pipeline looks
identical to a healthy one that's just between runs unless you're actually
watching how much WAL it's retaining.

This repo is that missing alert, built so it can be dropped in front of any
Postgres instance that uses logical replication slots — DMS-backed or
otherwise.

## How it decides what to alert on

The obvious signal — "is this slot `active`?" — is not the right one.
Slots legitimately sit inactive between runs for all sorts of healthy
reasons. The signal that actually matters is **how much WAL a slot is
forcing the server to retain**, regardless of whether it happens to be
active at the exact moment you check:

```sql
SELECT slot_name, active,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;
```

`monitor/handler.py` runs this query, flags any slot whose `retained_bytes`
crosses a configurable threshold (`WAL_RETENTION_ALERT_BYTES`, default
5 GiB), publishes a `ReplicationSlotRetainedBytes` CloudWatch metric per
slot for graphing, and — if any slot is over threshold — publishes directly
to an SNS topic (wire a PagerDuty integration, email, whatever, onto that
topic).

## Try it locally

The `demo/` directory reproduces the actual failure mode end to end against
a real Postgres instance, using the exact monitor code above — not a
reimplementation of it.

```bash
cd demo
docker compose up -d
pip install -r requirements.txt
python run_demo.py
```

What it does:

1. Creates `orphaned_dms_task_slot`, then writes 800 rows while it exists —
   standing in for a DMS task that was running, then stopped.
2. Creates `healthy_consumer_slot` *after* that traffic, representing a
   consumer that's currently caught up.
3. Runs `monitor.handler.run()` in dry-run mode and asserts it flags only
   the orphaned slot.
4. Drops the orphaned slot — the actual fix — and re-runs the monitor to
   confirm the retained WAL is gone.

It fails loudly (a Python `AssertionError`, non-zero exit code) if the
monitor doesn't discriminate correctly, so it also works as an integration
check, not just a demo.

## Deploying for real

```bash
./scripts/build_lambda.sh          # packages monitor/ into build/monitor.zip
cd terraform
terraform init
terraform apply \
  -var="db_host=your-instance.xxxxx.us-east-1.rds.amazonaws.com" \
  -var="db_name=your_db" \
  -var="db_user=a_read_only_user" \
  -var="db_password_secret_arn=arn:aws:secretsmanager:...:secret:..." \
  -var="notification_email=you@example.com"
```

This provisions:

- The Lambda (packaged by `build_lambda.sh`, `psycopg2-binary` pinned to
  Lambda's actual Linux runtime platform regardless of what OS you build
  on — see the comment in that script for why that matters)
- An EventBridge schedule (`rate(15 minutes)` by default)
- An SNS topic for alerts, optionally with an email subscription
- A least-privilege IAM role: `secretsmanager:GetSecretValue` scoped to
  exactly the one secret, `cloudwatch:PutMetricData`, `sns:Publish` scoped
  to exactly the one topic, plus VPC networking permissions only if you
  pass subnet/security group IDs (RDS is almost always in a VPC; the
  Lambda needs to be too, to reach it)
- A CloudWatch alarm on the *monitor's own* Lambda errors — if the check
  stops running, that's at least as important to know as a slot problem

`db_user` needs no special privileges beyond what's required to query
`pg_replication_slots`, which is readable by any role by default.

### A design decision worth calling out

There's deliberately no CloudWatch alarm on the `ReplicationSlotRetainedBytes`
metric itself. That metric carries a `SlotName` dimension, and CloudWatch
alarms can't wildcard across dimension values — an alarm without a slot
name fixed in advance would just never receive data and silently never
fire, which is exactly the kind of gap this project exists to close, not
recreate. The metric exists for graphing and ad hoc investigation; the
Lambda alerts directly to SNS when it detects a problem, since that path
has no such limitation.

## Repository layout

```text
pg-slot-sentinel/
├── monitor/
│   ├── handler.py          # the Lambda / the actual logic
│   ├── requirements.txt
│   └── tests/               # unit tests, mocked AWS + a fake DB cursor
├── terraform/                # Lambda, schedule, SNS, IAM, alarms
├── demo/                     # docker-compose Postgres + reproduction script
├── scripts/build_lambda.sh   # packages monitor/ for Lambda
└── .github/workflows/ci.yml  # pytest + terraform fmt/validate
```

## License

MIT
