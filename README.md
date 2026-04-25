# ikerp_metrics

Odoo addon that pushes a metrics snapshot of the running tenant to the
IKERP control plane every 15 minutes. Compatible with **Odoo 17.0** and
**Odoo 19.0**.

## Required environment variables

Injected by the IKERP orchestrator into the tenant container:

| Var                    | Example                                          |
| ---------------------- | ------------------------------------------------ |
| `IKERP_METRICS_URL`    | `https://app.ikerp.app/api/instances/metrics`    |
| `IKERP_INSTANCE_ID`    | Appwrite document id of the instance             |
| `IKERP_METRICS_TOKEN`  | 32-byte secret, unique per instance              |

If any one of the three is missing, the addon logs a single `INFO` line
and stays inert. No exceptions, no retries, no failed cron runs.

## What it sends

`POST <IKERP_METRICS_URL>` with headers:

```
Authorization: Bearer <IKERP_METRICS_TOKEN>
X-Instance-Id: <IKERP_INSTANCE_ID>
Content-Type: application/json
```

Body (all numeric fields default to 0 / `0.0` if their source module is
not installed or a partial collection error occurs):

```json
{
  "instanceId": "string",
  "collectedAt": "2026-04-25T18:00:00Z",
  "odooVersion": "17.0",
  "counters": {
    "activeUsers": 0,
    "totalUsers": 0,
    "products": 0,
    "documents": 0,
    "contacts": 0
  },
  "sales": {
    "byPeriod":   [{ "period": "2026-04", "amountTotal": 0.0, "orderCount": 0, "currency": "USD" }],
    "topProducts":[{ "productId": 0, "name": "string", "qtySold": 0.0, "amountTotal": 0.0 }]
  },
  "invoices": {
    "draft": 0, "posted": 0, "paid": 0, "overdue": 0, "totalReceivable": 0.0
  },
  "resources": {
    "cpuPercent": 0.0, "memUsedMB": 0, "memLimitMB": 0,
    "diskUsedMB": 0, "uptimeSeconds": 0
  }
}
```

## On-demand refresh

`POST /ikerp/metrics/push` on the tenant itself, with the same bearer
token, triggers an immediate collect-and-push. Useful when a user opens
the dashboard tile for an instance that just woke up.

| Status | Meaning                                                        |
| ------ | -------------------------------------------------------------- |
| 200    | Snapshot built and accepted by the control plane.              |
| 401    | Auth header missing / token mismatch / addon not configured.   |
| 502    | Snapshot built but control plane rejected or network failed.   |
| 500    | Unexpected error inside Odoo (logged with full traceback).     |

## Failure model

- Network errors: log at DEBUG, retry once with 1s+3s backoff, give up
  silently. Next cron run will try again.
- 4xx from control plane: log at WARN, do not retry. The token is bad
  or the instance was deprovisioned — retrying won't help.
- Per-section collection error (e.g. broken `top products` query):
  the failing block is replaced with its empty default and the rest of
  the snapshot still ships. See `_safe()` in `metrics_collector.py`.
- Missing env vars: single INFO log line, no further action.

## What gets counted

| Field                       | Source                                                                      |
| --------------------------- | --------------------------------------------------------------------------- |
| `counters.activeUsers`      | `res.users` where `active=True`, `share=False`, `id != 1`                   |
| `counters.totalUsers`       | `res.users` where `share=False`, `id != 1`                                  |
| `counters.products`         | `product.template` where `active=True`                                      |
| `counters.documents`        | `documents.document` if installed, else filtered `ir.attachment`            |
| `counters.contacts`         | `res.partner` where `active=True`                                           |
| `sales.byPeriod`            | `sale.order` in (`sale`,`done`), grouped by `YYYY-MM`, last 12 months       |
| `sales.topProducts`         | `sale.order.line` in (`sale`,`done`), last 90 days, top 10 by qty_delivered |
| `invoices.draft`            | `account.move` customer types in state `draft`                              |
| `invoices.posted`           | `account.move` customer types in state `posted`                             |
| `invoices.paid`             | posted, payment_state in (`paid`,`in_payment`)                              |
| `invoices.overdue`          | posted, due date past, payment_state in (`not_paid`,`partial`)              |
| `invoices.totalReceivable`  | sum of `amount_residual_signed` on posted `out_invoice`                     |
| `resources.cpuPercent`      | `psutil.Process(os.getpid()).cpu_percent(interval=1)`                       |
| `resources.memUsedMB`       | RSS of the Odoo worker process                                              |
| `resources.memLimitMB`      | cgroup v2 → cgroup v1 → host total                                          |
| `resources.diskUsedMB`      | `psutil.disk_usage('/var/lib/odoo')`                                        |
| `resources.uptimeSeconds`   | wall time since `psutil.Process.create_time()`                              |

## Installation

The addon ships in the IKERP tenant Docker image; no manual install is
needed. To install it manually for development:

```bash
# inside the Odoo container
odoo -d <db> -u ikerp_metrics --stop-after-init
```

External Python deps: `requests`, `psutil` (both ship with `odoo:19`).

## Testing the push manually

```bash
docker exec -it ikerp-<subdomain> bash -lc '
  python3 -c "
import xmlrpc.client, os
url = \"http://localhost:8069\"
db  = \"<db>\"
common = xmlrpc.client.ServerProxy(f\"{url}/xmlrpc/2/common\")
uid = common.authenticate(db, \"admin\", \"admin\", {})
models = xmlrpc.client.ServerProxy(f\"{url}/xmlrpc/2/object\")
print(models.execute_kw(db, uid, \"admin\",
    \"ikerp.metrics.collector\", \"_collect\", []))
"
'
```

Or hit the on-demand endpoint:

```bash
curl -X POST https://<subdomain>.ikerp.app/ikerp/metrics/push \
     -H "Authorization: Bearer $IKERP_METRICS_TOKEN"
```
