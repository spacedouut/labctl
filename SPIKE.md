# SPIKE — phase v4: Python rewrite + template pipeline + TUI + engine

Date: 2026-08-06 · Branch: `v4` (draft PR) · Status: spike, not production

## Verdict: VALIDATED (core) / PARTIAL (TUI, engine daemon)

**Question:** Can phase's bash CLI be rewritten in pure-stdlib Python with the
copyparty-style optional-extras model, a resumable template pipeline, an
engine capability scanner, a Textual TUI, and no loss of v3 behavior?

**Evidence:** 60/60 automated smoke tests pass against a fake `qm` (all
commands, template create→finish→abort, dry-run, plan CRUD, ansible
inventory, engine scan/doctor/daemon RPC, headless TUI navigation). Read-only
validation against the **real PVE host** (homelab, via SSH transport):
`vm list` (live guest-agent IPs), `vm status`, `templates`, `nextid`,
`engine scan` (4 storage pools, 2 bridges, 18 vGPU mdev types with correct
allocation — `nvidia-257: 11 avail (2 in use → 9 free)`), `inventory --list`.

## What worked

- **Hybrid grammar ported 1:1** (verb-first / noun-first / legacy / bare-name
  ssh). Scripts written against v3 keep working.
- **Template pipeline** (`create → awaiting-config checkpoint → finish →
  abort`) with persisted JSON state — the human-in-the-loop step ("give ssh
  before cleanup") is the part bash could never do sanely.
- **Engine scan** with real-world data: pvesh node status, pvesm storage,
  bridges, `mdevctl types` (both flat and PCI-scoped formats), `nvidia-smi
  vgpu` active instances, per-mdev allocation tracking.
- **SSH transport** (`--host root@<pve>` / `PHASE_HOST`): everything tunnels
  over ssh; read-only ops validated from hermes against homelab.
- **Extras registry** (`phase extras`): tui/notify/report/api pip extras +
  binary extras, graceful `MissingExtra` with install hints.
- **Event log** (JSONL), plan library CRUD, disk id+role model, ansible
  inventory, snapshots, backups, ephemeral ssh keys (gcloud-style).

## What failed or surprised us

- **`qm list` has a single space between VMID and name** (right-aligned
  columns) — split-on-2-spaces broke; switched to a regex row parser.
- **pvesh returns `loadavg` as a string**, not a list.
- **`mdevctl types` output differs across hosts** (flat `key=value` vs
  PCI-scoped `Key: value`) — parser handles both.
- **Textual gotcha:** a screen method named `refresh()` shadows Textual's
  internal `Widget.refresh()` → NoMatches crashes during mount. Renamed to
  `load()`. Worth remembering for the real build.
- **`--host` global flag greedily ate `inventory --host <name>`** — global
  flags now only parse before the subcommand.
- TUI is only **headless-tested** (keyboard navigation + screen transitions).
  No human has sat in front of it yet; fonts/emoji/layout need eyeballs.

## Not validated (deferred or needs human)

- Mutating ops against a real PVE host (create/provision/template finish/
  destroy) — fake-qm only. The real-host runs were strictly read-only.
- `[api]` proxmoxer transport (code present, no API token to test against).
- `[report]` jinja2 output (dep installed, HTML not eyeballed).
- Engine daemon long-run behavior (tested: starts, binds socket, answers
  RPC, SIGTERM clean).
- v3 parity is behavioral, not byte-for-byte — the plan schema changed
  (single `disk` → `disks[]` array) by design.

## Recommendation

Ship as a **draft PR** for beta review, per Asher's ask. Next production
steps before merge-worthy:
1. Human TUI pass (layout, colors, focus flow).
2. One real-host provision + template create/finish cycle on a scratch VM.
3. Add CI (lint + `tests/run_tests.py`) to the PR.
4. Decide ephemeral-ssh default behavior.
