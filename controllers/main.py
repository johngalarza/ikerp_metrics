# -*- coding: utf-8 -*-
"""On-demand metrics push endpoint.

Exposes ``POST /ikerp/metrics/push`` so the IKERP control plane can request
a fresh snapshot without waiting for the next cron tick (e.g. right after
a user opens the dashboard tile of an instance that has been idle for a
while).

Auth model: the request must carry the same ``Authorization: Bearer <token>``
that the cron uses outbound. The token is the one injected by the
orchestrator via ``IKERP_METRICS_TOKEN``. Constant-time comparison.

This endpoint never returns Odoo internals on error — only generic JSON
status codes.
"""

import json
import logging

from odoo import http
from odoo.http import request, Response

_logger = logging.getLogger(__name__)


def _json_response(payload, status=200):
    return Response(
        json.dumps(payload),
        status=status,
        content_type="application/json",
    )


class IkerpMetricsController(http.Controller):

    @http.route(
        "/ikerp/metrics/push",
        type="http",
        auth="none",
        methods=["POST"],
        csrf=False,
    )
    def push_metrics(self, **_kwargs):
        env = request.env
        collector = env["ikerp.metrics.collector"].sudo()

        auth_header = request.httprequest.headers.get("Authorization", "")
        if not collector._verify_bearer(auth_header):
            # Don't disclose whether config is missing vs token is wrong.
            return _json_response({"ok": False, "error": "unauthorized"}, status=401)

        try:
            ok = collector._push()
        except Exception:
            _logger.exception("ikerp_metrics: on-demand push raised")
            return _json_response({"ok": False, "error": "internal_error"}, status=500)

        if not ok:
            # Push was attempted but control plane rejected or network failed.
            # We still return 200 so the caller knows the endpoint is alive;
            # the body conveys the outcome.
            return _json_response({"ok": False, "error": "push_failed"}, status=502)

        return _json_response({"ok": True}, status=200)
