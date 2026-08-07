# phase — v4 (Python rewrite)

GCP Compute Engine-style VM management for Proxmox VE. `phase` is a CLI (with
a Textual TUI) that wraps `qm`/`pvesh`/`vzdump` and adds the orchestration
layer: plans (instance templates), a resumable template pipeline, engine
capability scans, disk/GPU management, and an event log.

**v4 is a draft rewrite of the v3 bash CLI in pure-stdlib Python.** See
[SPIKE.md](SPIKE.md) for the verdict and what was validated.

## Quick start

```bash
# on the PVE host (or any machine — see --host)
git clone --branch v4 https://github.com/spacedouut/phase.git /opt/phase
bash /opt/phase/installer.sh --extra tui        # venv + wrapper + (optionally) engine service
phase vm list
phase                                            # TUI menu (needs the tui extra)
```

No install needed for the core: `python3 bin/phase <args>` runs anywhere.

## Grammar (v3-compatible hybrid)

```bash
phase vm plan|create|provision|nextid ...   # verb-first (creation)
phase vm <name> <action> ...                # noun-first (per-VM ops)
phase vm <name>                             # bare name = ssh
phase vm <name> shell|exec|service|logs|status|start|stop|reboot|shutdown
phase vm <name> tag|firewall|rename|destroy|bootstrap
phase vm <name> disk <add|list|resize|detach>
phase vm <name> snapshot <create|list|rollback> | backup | resize | wait | info | ip | agent
phase template create|finish|abort|list|show
phase engine scan|doctor|daemon|install|uninstall|status|logs|jobs
phase plan list|show|rm|export|import
phase ssh-key add|remove|list
phase inventory --list | --host <name>      # ansible dynamic inventory
phase report                                # HTML report (needs [report] extra)
phase extras                                # what's installed / installable
```

Global flags (before the subcommand): `--config <file>` `--host <ssh-host>`
`--json`.

## The template pipeline

The headline feature — GCP image-family semantics with a human checkpoint:

```bash
phase template create --name tpl-ubuntu-26-docker --from tpl-ubuntu-26 --system
#   → clones + boots + bootstraps, then PAUSES with an ssh hint
ssh ubuntu@10.10.1.x                        # configure to your liking
phase template finish tpl-ubuntu-26-docker   # cleanup → minimize → save
phase template abort tpl-ubuntu-26-docker    # give up (destroy)
```

`finish` runs guest cleanup (fstrim, apt clean), shuts down, minimizes
(1 core / micro RAM / balloon / `fstrim_cloned_disks=1`), and runs
`qm template`. State persists in `/var/lib/phase/templates/<name>.json`, so
the pipeline survives reboots and is resumable.

## Disks — the id+role model

Disks are keyed by PVE id (`scsi0`) and carry a role:

```bash
phase vm create app --disk scsi0:os:ubuntu-26:32G --disk scsi1:data::100G:nas
phase vm app disk list          # id | role | storage | size | model
phase vm app disk add --id scsi2 --role data --size 50G --storage local-lvm
phase vm app disk resize scsi1 200G     # grow-only, guarded
phase vm app disk detach scsi2          # refuses the os disk
```

## Engine

```bash
phase engine scan        # host, storage, bridges, GPU/mdev types + allocation
phase engine doctor      # config + dependency checks before attaching things
phase engine install     # systemd service (phase-engine.service)
phase engine daemon      # scheduled jobs + unix-socket RPC + event log
phase engine jobs        # list / run jobs
```

`engine scan` discovers vGPU types from `mdevctl types` + `nvidia-smi vgpu`,
shows what's attached to which VM, and reports remaining capacity:

```
  nvidia-257       GRID RTX6000-2Q        avail 11 ( 2 in use → 9 free)
  attached:   hermes (VMID 103) → nvidia-257
```

Provision them with `phase vm create ... --gpu nvidia-257`.

## Optional extras (copyparty-style)

The core is pure stdlib. Optional pip extras unlock features; `phase extras`
shows the full table:

| extra     | packages            | unlocks |
|-----------|---------------------|---------|
| `tui`     | textual, rich       | menu UI (`phase`), rich tables/prompts |
| `notify`  | apprise             | push notifications on long ops |
| `report`  | jinja2              | `phase report` HTML output |
| `api`     | proxmoxer           | REST API transport (falls back to qm) |

Install: `uv pip install -e '.[tui,notify]'` in the checkout, or
`pip install phase[tui]`. Binary extras (rclone, zstd, gum) are detected via
`which` and used when present.

## Config

`/etc/phase.json` (override: `PHASE_CONFIG` env or `--config`). See
[config.schema.json](config.schema.json) for the full v4 schema — new in v4:
`default_storage`, `gpus`, `remote.host`, `notify`, `api`, `backup`,
`engine.jobs`.

## State

`/var/lib/phase/` (override: `PHASE_STATE_DIR`):
`plans/` saved plans · `templates/` in-flight template pipeline state ·
`events.jsonl` event log · `engine-state.json` job timestamps.

## Installer / update

```bash
bash installer.sh [--extra tui] [--extra notify] [--engine] [--no-start]
phase update            # fetch v4 + reinstall (extras preserved)
```

Both the installer and local dev use the same venv logic via
`scripts/phase-env.sh` (uv-first, `python3 -m venv` fallback).

## Development

```bash
uv sync --all-extras          # or: --extra tui --extra notify
.venv/bin/python tests/run_tests.py    # 60 smoke tests against a fake qm
# real-host read-only check (no mutations):
.venv/bin/python bin/phase --config /etc/phase.json --host root@<pve> vm list
```
