"""HTML status report — `phase report` (needs the optional [report] extra)."""

from __future__ import annotations

import json

from .extras import require
from .log import tail_events
from .plan import parse_flags
from .util import die, log
from .vm import all_vms

_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>phase report</title>
<style>
 body { font-family: ui-monospace, monospace; margin: 2rem; color: #222; }
 h1 { border-bottom: 2px solid #444; padding-bottom: .4rem; }
 table { border-collapse: collapse; margin: 1rem 0; }
 th, td { border: 1px solid #bbb; padding: .3rem .7rem; text-align: left; }
 th { background: #eee; }
 .running { color: #0a7d32; font-weight: bold; } .stopped { color: #a00; }
 .ok { color: #0a7d32; } .err { color: #a00; }
 .muted { color: #777; }
</style></head><body>
<h1>phase report <span class="muted">— {{ generated }}</span></h1>
<h2>VMs ({{ vms|length }})</h2>
<table>
<tr><th>VMID</th><th>Name</th><th>Status</th><th>IP</th><th>Tags</th><th>Cores</th><th>Mem (MB)</th></tr>
{% for v in vms %}
<tr><td>{{ v.vmid }}</td><td>{{ v.name }}</td>
<td class="{{ v.status }}">{{ v.status }}</td><td>{{ v.ip or '-' }}</td>
<td>{{ v.tags or '-' }}</td><td>{{ v.cores }}</td><td>{{ v.memory }}</td></tr>
{% endfor %}
</table>
<h2>Events (last {{ events|length }})</h2>
<table><tr><th>Time</th><th>Event</th><th>Detail</th></tr>
{% for e in events %}
<tr><td>{{ e.ts }}</td><td>{{ e.event }}</td><td>{{ e.name or e.detail or e.job or '' }}</td></tr>
{% endfor %}
</table>
</body></html>
"""


def cmd_report(cfg, qm, argv):
    opts, _ = parse_flags(argv, {"out": {"default": ""}})
    try:
        from jinja2 import Template
    except ImportError:
        from .extras import MissingExtra
        raise MissingExtra("report")
    vms = [v for v in all_vms(qm, cfg) if not v["template"]]
    events = tail_events(50)
    html = Template(_TEMPLATE).render(
        vms=vms, events=events,
        generated=__import__("time").strftime("%Y-%m-%d %H:%M"),
    )
    if opts["out"]:
        with open(opts["out"], "w") as f:
            f.write(html)
        log(f"Report written to {opts['out']}")
    else:
        log(html)
    return 0
