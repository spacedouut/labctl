"""Ansible dynamic inventory — `phase inventory --list` / `--host <name>`.

Groups: all, env:<prefix>, tag:<tag>. Templates excluded unless
--include-templates. Pure stdlib JSON on stdout, so it slots straight into
`ansible -i phase --list-hosts` style workflows (the binary must be on PATH).
"""

from __future__ import annotations

import json

from .plan import parse_flags
from .util import die, log
from .vm import all_vms


def _hostvars(cfg, row: dict) -> dict:
    vars_ = {
        "phase_vmid": row["vmid"],
        "phase_status": row["status"],
        "phase_tags": [t for t in row["tags"].split(";") if t],
        "phase_memory_mb": row["memory"],
        "phase_cores": row["cores"],
    }
    if row["ip"]:
        vars_["ansible_host"] = row["ip"]
    return vars_


def inventory_payload(cfg, qm, include_templates: bool = False) -> dict:
    groups = {"all": {"hosts": []}, "_meta": {"hostvars": {}}}
    for row in all_vms(qm, cfg):
        if row["template"] and not include_templates:
            continue
        groups["all"]["hosts"].append(row["name"])
        groups["_meta"]["hostvars"][row["name"]] = _hostvars(cfg, row)
        for t in row["tags"].split(";"):
            if t:
                groups.setdefault(f"tag:{t}", {"hosts": []})["hosts"].append(row["name"])
        env = row["name"].split("-")[0]
        groups.setdefault(f"env:{env}", {"hosts": []})["hosts"].append(row["name"])
    if include_templates:
        groups["templates"] = {
            "hosts": [r["name"] for r in all_vms(qm, cfg) if r["template"]]}
    return groups


def cmd_inventory(cfg, qm, argv):
    opts, pos = parse_flags(argv, {
        "list": {"bool": True, "default": False},
        "host": {"default": ""},
        "include-templates": {"bool": True, "default": False},
    })
    if opts["host"]:
        name = opts["host"]
        for row in all_vms(qm, cfg):
            if row["name"] == name:
                log(json.dumps(_hostvars(cfg, row), indent=2))
                return 0
        die(f"unknown host: {name}")
    log(json.dumps(inventory_payload(cfg, qm, opts["include-templates"]), indent=2))
    return 0
