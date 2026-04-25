# -*- coding: utf-8 -*-
{
    "name": "IKERP Metrics",
    "version": "1.0.0",
    "summary": "Push tenant metrics snapshots to the IKERP control plane.",
    "description": """
IKERP Metrics
=============

Collects a snapshot of usage and resource metrics from this Odoo instance
and POSTs it to the IKERP control plane every 15 minutes. Designed to run
silently inside an IKERP-provisioned tenant container.

Configuration is read from the container environment:

- IKERP_METRICS_URL    Full HTTPS endpoint (e.g. https://app.ikerp.app/api/instances/metrics)
- IKERP_INSTANCE_ID    Appwrite document id of this instance
- IKERP_METRICS_TOKEN  Bearer token issued at provisioning time

If any of the three are missing, the module logs a single info line and
stays out of the way — no errors, no retries, no exceptions bubbling up.

Compatible with Odoo 17.0 and 19.0.
""",
    "author": "IKERP",
    "website": "https://ikerp.app",
    "license": "LGPL-3",
    "category": "Technical",
    "depends": ["base"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
    ],
    "external_dependencies": {
        "python": ["psutil", "requests"],
    },
    "installable": True,
    "application": False,
    "auto_install": False,
}
