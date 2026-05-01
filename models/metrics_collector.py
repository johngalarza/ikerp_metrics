# -*- coding: utf-8 -*-
"""IKERP metrics collector.

Builds a JSON snapshot of usage / resource metrics for the running Odoo
tenant and POSTs it to the IKERP control plane. The collector is wrapped
in defensive try/except blocks at every level — a partial failure in one
section (e.g. top products SQL) must NEVER abort the whole snapshot, and
network errors must NEVER bubble up into the cron worker.
"""

import functools
import hmac
import json
import logging
import os
import time
import subprocess
from datetime import datetime, timedelta

import odoo
from odoo import api, models, release

_logger = logging.getLogger(__name__)

# Lazy import flags — declared at module level so we fail soft if the host
# image is missing one of the optional deps. The manifest declares both as
# external_dependencies, but the official odoo:19 image already ships them.
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None
    _logger.warning("ikerp_metrics: 'requests' is not installed; HTTP push disabled")

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None
    _logger.warning("ikerp_metrics: 'psutil' is not installed; resource block will be empty")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe(default=None):
    """Decorator: any exception inside the wrapped method is logged at DEBUG
    and the decorator returns ``default`` instead of propagating. Use this
    on each block builder so a single broken section does not nuke the
    full snapshot.
    """
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, *args, **kwargs):
            try:
                return fn(self, *args, **kwargs)
            except Exception:
                _logger.exception("ikerp_metrics: %s failed, returning default", fn.__name__)
                return default
        return wrapper
    return deco


def _read_int_file(path):
    """Read a single integer from a sysfs/cgroup file, return None on any error."""
    try:
        with open(path, "r") as f:
            value = f.read().strip()
        if value in ("", "max"):
            return None
        return int(value)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------

class MetricsCollector(models.AbstractModel):
    _name = "ikerp.metrics.collector"
    _description = "IKERP Metrics Collector"

    # ------------------------------------------------------------------ env

    @api.model
    def _get_config(self):
        """Read the three env vars injected by the IKERP orchestrator.

        Returns a tuple ``(url, instance_id, token)`` or ``None`` if any of
        them is missing. The caller is expected to short-circuit silently
        in that case.
        """
        url = os.environ.get("IKERP_METRICS_URL", "").strip()
        instance_id = os.environ.get("IKERP_INSTANCE_ID", "").strip()
        token = os.environ.get("IKERP_METRICS_TOKEN", "").strip()
        if not (url and instance_id and token):
            return None
        return url, instance_id, token

    # ------------------------------------------------------------------ counters

    @api.model
    @_safe(default={})
    def _collect_counters(self):
        Users = self.env["res.users"].sudo()
        Partner = self.env["res.partner"].sudo()
        Product = self.env["product.template"].sudo()
        Attachment = self.env["ir.attachment"].sudo()

        # Real users: not a portal/share user, not the OdooBot/admin id=1.
        user_domain = [("share", "=", False), ("id", "!=", 1)]
        total_users = Users.search_count(user_domain)
        active_users = Users.search_count(user_domain + [("active", "=", True)])

        products = Product.search_count([("active", "=", True)])
        contacts = Partner.search_count([("active", "=", True)])

        # If the optional `documents` module is installed, prefer it; the
        # default ir.attachment table is polluted with binary-field blobs
        # and assets that aren't user "documents" in any meaningful sense.
        documents = 0
        if "documents.document" in self.env:
            try:
                documents = self.env["documents.document"].sudo().search_count([])
            except Exception:
                _logger.debug("ikerp_metrics: documents.document unavailable", exc_info=True)
        if not documents:
            documents = Attachment.search_count([
                ("res_field", "=", False),
                ("res_model", "!=", "ir.ui.view"),
            ])

        return {
            "activeUsers": active_users,
            "totalUsers": total_users,
            "products": products,
            "documents": documents,
            "contacts": contacts,
        }

    # ------------------------------------------------------------------ sales

    @api.model
    @_safe(default={"byPeriod": [], "topProducts": []})
    def _collect_sales(self):
        if "sale.order" not in self.env:
            # Sales module not installed on this tenant.
            return {"byPeriod": [], "topProducts": []}

        cr = self.env.cr
        company = self.env.company
        currency = (company.currency_id.name if company and company.currency_id else "USD")

        # ---- byPeriod: last 12 months, grouped by YYYY-MM
        twelve_months_ago = (datetime.utcnow().replace(day=1) - timedelta(days=370))
        cr.execute(
            """
            SELECT to_char(date_order, 'YYYY-MM') AS period,
                   COALESCE(SUM(amount_total), 0.0)  AS amount_total,
                   COUNT(*)                          AS order_count
              FROM sale_order
             WHERE state IN ('sale', 'done')
               AND date_order >= %s
          GROUP BY period
          ORDER BY period ASC
            """,
            (twelve_months_ago,),
        )
        by_period = [
            {
                "period": row[0],
                "amountTotal": float(row[1] or 0.0),
                "orderCount": int(row[2] or 0),
                "currency": currency,
            }
            for row in cr.fetchall()
        ]

        # ---- topProducts: last 90 days, top 10 by qty_delivered
        ninety_days_ago = datetime.utcnow() - timedelta(days=90)
        cr.execute(
            """
            SELECT sol.product_id            AS product_id,
                   COALESCE(SUM(sol.qty_delivered), 0.0) AS qty_sold,
                   COALESCE(SUM(sol.price_subtotal), 0.0) AS amount_total
              FROM sale_order_line sol
              JOIN sale_order so ON so.id = sol.order_id
             WHERE so.state IN ('sale', 'done')
               AND so.date_order >= %s
               AND sol.product_id IS NOT NULL
          GROUP BY sol.product_id
          ORDER BY qty_sold DESC
             LIMIT 10
            """,
            (ninety_days_ago,),
        )
        rows = cr.fetchall()
        top_products = []
        if rows:
            product_ids = [r[0] for r in rows]
            products = self.env["product.product"].sudo().browse(product_ids)
            name_by_id = {p.id: p.display_name for p in products}
            for product_id, qty_sold, amount_total in rows:
                top_products.append({
                    "productId": int(product_id),
                    "name": name_by_id.get(product_id, ""),
                    "qtySold": float(qty_sold or 0.0),
                    "amountTotal": float(amount_total or 0.0),
                })

        return {"byPeriod": by_period, "topProducts": top_products}

    # ------------------------------------------------------------------ invoices

    @api.model
    @_safe(default={
        "draft": 0, "posted": 0, "paid": 0, "overdue": 0, "totalReceivable": 0.0,
    })
    def _collect_invoices(self):
        if "account.move" not in self.env:
            return {"draft": 0, "posted": 0, "paid": 0, "overdue": 0, "totalReceivable": 0.0}

        Move = self.env["account.move"].sudo()
        customer_types = [("move_type", "in", ("out_invoice", "out_refund"))]

        draft = Move.search_count(customer_types + [("state", "=", "draft")])
        posted = Move.search_count(customer_types + [("state", "=", "posted")])
        paid = Move.search_count(
            customer_types + [("state", "=", "posted"), ("payment_state", "in", ("paid", "in_payment"))]
        )
        today = odoo.fields.Date.context_today(self)
        overdue = Move.search_count(
            customer_types + [
                ("state", "=", "posted"),
                ("invoice_date_due", "<", today),
                ("payment_state", "in", ("not_paid", "partial")),
            ]
        )

        # Sum residual on posted out_invoice only (out_refund cancels it).
        receivable_records = Move.search([
            ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"),
        ])
        total_receivable = sum(receivable_records.mapped("amount_residual_signed") or [0.0])

        return {
            "draft": draft,
            "posted": posted,
            "paid": paid,
            "overdue": overdue,
            "totalReceivable": float(total_receivable),
        }

    # ------------------------------------------------------------------ resources

    @api.model
    @_safe(default={
        "cpuPercent": 0.0, "memUsedMB": 0, "memLimitMB": 0,
        "diskUsedMB": 0, "uptimeSeconds": 0,
    })
    def _collect_resources(self):
        if psutil is None:
            return {
                "cpuPercent": 0.0, "memUsedMB": 0, "memLimitMB": 0,
                "diskUsedMB": 0, "uptimeSeconds": 0,
            }

        proc = psutil.Process(os.getpid())

        # cpu_percent with interval=1 blocks for 1s but gives a real reading.
        cpu_percent = float(proc.cpu_percent(interval=1.0))

        # Memory used: RSS of this Odoo worker. Whole-container mem is
        # cgroup-bound and would require iterating sibling pids; the worker
        # RSS is a reasonable proxy and what an operator typically watches.
        mem_used_mb = int(proc.memory_info().rss / (1024 * 1024))

        # Memory limit: cgroup v2 first, then v1 fallback, then host total.
        mem_limit_bytes = (
            _read_int_file("/sys/fs/cgroup/memory.max")
            or _read_int_file("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        )
        if mem_limit_bytes is None or mem_limit_bytes <= 0:
            mem_limit_bytes = psutil.virtual_memory().total
        # Some hosts report an absurd "no limit" sentinel (~ 9.2e18). Cap.
        if mem_limit_bytes > (1 << 50):
            mem_limit_bytes = psutil.virtual_memory().total
        mem_limit_mb = int(mem_limit_bytes / (1024 * 1024))

        # Disk used by this Odoo tenant: DB (pg_database_size) + filestore.
        # Mirrors ikerp.storage so both modules report the same number.
        try:
            db_bytes = self._measure_db_bytes()
            fs_bytes = self._measure_filestore_bytes()
            disk_used_mb = _bytes_to_mb_ceil(db_bytes + fs_bytes)
        except Exception:
            disk_used_mb = 0

        uptime_seconds = int(time.time() - proc.create_time())

        return {
            "cpuPercent": round(cpu_percent, 2),
            "memUsedMB": mem_used_mb,
            "memLimitMB": mem_limit_mb,
            "diskUsedMB": disk_used_mb,
            "uptimeSeconds": uptime_seconds,
        }

    # ------------------------------------------------------------------ snapshot

    @api.model
    def _collect(self):
        """Build the complete snapshot dict. Always returns a dict — never raises."""
        config = self._get_config()
        instance_id = config[1] if config else os.environ.get("IKERP_INSTANCE_ID", "")

        return {
            "instanceId": instance_id,
            "collectedAt": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "odooVersion": release.major_version,  # "17.0" / "19.0"
            "counters": self._collect_counters() or {},
            "sales": self._collect_sales() or {"byPeriod": [], "topProducts": []},
            "invoices": self._collect_invoices() or {},
            "resources": self._collect_resources() or {},
        }

    # ------------------------------------------------------------------ push

    @api.model
    def _push(self, payload=None):
        """POST the snapshot to IKERP. Returns True on 2xx, False otherwise.

        Never raises. One exponential retry (1s then 3s) on connection errors
        or 5xx. 4xx responses are not retried — the token is bad or the
        instance was deprovisioned, both of which are not fixed by retrying.
        """

        config = self._get_config()
        if config is None:
            _logger.info("ikerp_metrics: not configured, skipping")
            return False
        if requests is None:
            _logger.warning("ikerp_metrics: 'requests' missing, cannot push")
            return False

        url, instance_id, token = config

        if payload is None:
            payload = self._collect()

        headers = {
            "Authorization": "Bearer " + token,
            "X-Instance-Id": instance_id,
            "Content-Type": "application/json",
            "User-Agent": "ikerp-metrics/1.0 (Odoo %s)" % release.major_version,
        }
        body = json.dumps(payload, default=str)

        backoffs = [1.0, 3.0]  # one retry => two total attempts
        last_error = None
        for attempt, sleep_after in enumerate(backoffs):
            try:
                response = requests.post(url, data=body, headers=headers, timeout=10)
            except requests.RequestException as exc:
                last_error = exc
                _logger.debug("ikerp_metrics: push attempt %s failed: %s", attempt + 1, exc)
            else:
                if 200 <= response.status_code < 300:
                    _logger.debug("ikerp_metrics: snapshot accepted by control plane")
                    return True
                if 400 <= response.status_code < 500:
                    # Token rejected, instance unknown, schema mismatch.
                    # Don't retry, don't raise — log and bail.
                    _logger.warning(
                        "ikerp_metrics: control plane returned %s; will retry on next cron",
                        response.status_code,
                    )
                    return False
                last_error = "HTTP %s" % response.status_code
                _logger.debug(
                    "ikerp_metrics: push attempt %s got %s, retrying",
                    attempt + 1, response.status_code,
                )

            if attempt < len(backoffs) - 1:
                time.sleep(sleep_after)

        _logger.info(
            "ikerp_metrics: push failed after retries (%s); will try again next cron",
            last_error,
        )
        return False

    # ------------------------------------------------------------------ cron entry

    @api.model
    def _cron_push_metrics(self):
        """Cron entry point. Wrapped one more time so even a programming
        error in _collect doesn't poison the scheduler."""
        try:
            self._push()
        except Exception:
            _logger.exception("ikerp_metrics: unexpected error in cron push")
        return True

    # ------------------------------------------------------------------ token check

    @api.model
    def _verify_bearer(self, header_value):
        """Constant-time comparison of a 'Bearer <token>' header against
        the configured token. Returns True on match, False otherwise.
        Never logs the token or any prefix of it.
        """
        config = self._get_config()
        if config is None or not header_value:
            return False
        expected = config[2]
        if not header_value.startswith("Bearer "):
            return False
        provided = header_value[len("Bearer "):].strip()
        # hmac.compare_digest needs equal-length-ish inputs to be useful;
        # it short-circuits on length mismatch but doesn't leak content.
        return hmac.compare_digest(provided, expected)

    def _measure_db_bytes(self):
        self.env.cr.execute("SELECT pg_database_size(current_database())")
        row = self.env.cr.fetchone()
        return int(row[0]) if row and row[0] is not None else 0

    def _measure_filestore_bytes(self):
        path = tools.config.filestore(self.env.cr.dbname)
        if not path or not os.path.isdir(path):
            return 0
        try:
            proc = subprocess.run(
                ["du", "-sb", path],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return int(proc.stdout.split()[0])
            _logger.warning(
                "ikerp_metrics: du -sb %s returned %s; falling back to os.walk. stderr=%s",
                path, proc.returncode, proc.stderr[:200],
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, OSError) as exc:
            _logger.warning(
                "ikerp_metrics: du failed (%s); falling back to os.walk for %s.",
                exc, path,
            )
        total = 0
        for dirpath, _dirs, files in os.walk(path):
            for fname in files:
                fpath = os.path.join(dirpath, fname)
                try:
                    total += os.path.getsize(fpath)
                except OSError:
                    continue
        return total