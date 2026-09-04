# pg-slot-sentinel

A watchdog for a specific, easy-to-miss way Postgres runs out of disk:
a logical replication slot nobody is reading from anymore, holding the
server hostage to old WAL it will never get to delete.

## Why this is worth watching for

Postgres will keep a replication slot's starting point pinned forever if
nothing ever advances it. That's the whole contract of a slot — it exists
so a consumer can disconnect and reconnect later without losing its place.
The trade-off is that Postgres can't tell the difference between "my
consumer is on a coffee break" and "my consumer was decommissioned three
months ago and nobody told me." Both look identical from the server's
side: an idle slot, and WAL piling up behind it.

This is a well-known trap with tools like AWS DMS in particular. Stopping
or deleting a DMS replication task cleans up the task itself, but the
logical slot it created on the source database is a separate object that
has to be dropped on purpose. Skip that step and the slot just sits there,
inactive, pinning WAL indefinitely — often for weeks before anyone
notices free storage quietly draining away with no matching growth in
actual table data.

By the time it shows up as a storage alarm, the fix is usually simple
(drop the slot, reclaim the space) but the diagnosis isn't, because
nothing points you at replication slots specifically. Disk usage graphs
just show a downward slope with no obvious cause.

## What this repo actually does

`monitor/` is a small Lambda that periodically asks Postgres how much WAL
each of its replication slots is pinning, flags any slot over a
configurable size, and pages out through SNS before "slowly draining"
turns into "out of storage."

The detection query is intentionally not "is this slot active":

```sql
SELECT slot_name, active,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS retained_bytes
FROM pg_replication_slots;
```

`active` tells you whether something is connected right now, which is a
weak, noisy signal — plenty of perfectly healthy slots are idle between
runs. `retained_bytes` tells you the thing that actually costs you disk
space, independent of whether the slot happens to be connected at the
instant you check. A slot can be `active = false` and completely fine, or
`active = false` and quietly eating a terabyte. Only one of those needs a
page.

`monitor/handler.py` runs that query, splits slots into "under threshold"
and "over threshold" (`WAL_RETENTION_ALERT_BYTES`, 5 GiB by default),
writes a `ReplicationSlotRetainedBytes` metric per slot to CloudWatch for
graphing, and publishes to an SNS topic when anything crosses the line —
hang PagerDuty, email, whatever off that topic.

## Proving it, not just describing it

`demo/` spins up a real Postgres container and walks through the actual
mechanics rather than asking you to take the README's word for it:

```bash
cd demo
docker compose up -d
pip install -r requirements.txt
python run_demo.py
```

It creates one replication slot and generates traffic while it exists but
nothing reads from it (a stand-in for a task that got stopped and
forgotten), then creates a second slot afterward that starts out fully
caught up (a stand-in for a consumer that's actually keeping up). It runs
the real `monitor.handler.run()` — not a mock of it — against both, and
asserts the first gets flagged and the second doesn't. Then it drops the
orphaned slot and re-checks to confirm the retained WAL is actually gone,
which is the real remediation step, not a simulated one.

If the monitor ever stops discriminating correctly between the two, this
script fails loudly (`AssertionError`, non-zero exit), so it doubles as a
regression check.

## Shipping it

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

That gives you:

- The Lambda itself, with `psycopg2-binary`'s compiled extensions built
  for Lambda's actual Linux runtime regardless of what machine you run
  the build script on (see the comment in `build_lambda.sh` — building a
  binary dependency on a Mac and shipping it to Lambda is a classic way
  to get an unhelpful import error at runtime)
- An EventBridge schedule running the check every 15 minutes by default
- An SNS topic, optionally wired to an email subscription out of the box
- IAM scoped tight: `secretsmanager:GetSecretValue` on exactly one
  secret, `sns:Publish` on exactly one topic, `cloudwatch:PutMetricData`,
  and VPC networking permissions only if you actually pass subnet/security
  group IDs (which you should — RDS lives in a VPC, so the Lambda needs
  to as well)
- A CloudWatch alarm on the Lambda's own error count, because a monitor
  that silently stops running is its own kind of outage

The database user only needs whatever privileges let it read
`pg_replication_slots`, which is world-readable by default — no elevated
access required.

### One thing that's deliberately missing

There's no CloudWatch alarm on `ReplicationSlotRetainedBytes` itself. That
metric is published per-slot with a `SlotName` dimension, and CloudWatch
alarms can't be defined to match "any value of this dimension" — an alarm
without a specific slot name baked in would just sit there receiving no
data and never trip, which would be exactly the kind of silent gap this
project is trying to eliminate. The metric is there for dashboards and
manual digging; the actual alert is the Lambda's direct SNS publish,
which doesn't have that limitation.

## Layout

```text
pg-slot-sentinel/
├── monitor/
│   ├── handler.py          # the check itself
│   ├── requirements.txt
│   └── tests/               # mocked AWS calls + a fake DB cursor
├── terraform/                # Lambda, schedule, SNS, IAM, alarms
├── demo/                     # docker-compose Postgres + the reproduction script
├── scripts/build_lambda.sh   # packages monitor/ for Lambda
└── .github/workflows/ci.yml  # pytest + terraform fmt/validate
```

## License

MIT
