# phase v3

Mostly done:
- [x] split into `tools.sh` / `cmds.sh` / `phase.sh`
- [x] pipefail everywhere
- [x] naming: user's own scheme, strictified (alphanumeric, lowercase, hyphens)
- [x] templating: sizes defined in config, not in template names
- [x] command renaming: `create` -> `provision` (kept both), `connect` -> `shell`
- [x] noun-first grammar: `phase vm <name> <action>` (GCP-style), legacy
      verb-first still works, bare `phase vm <name>` = shell
- [x] new: `service`, `logs`, `exec`, `status`, `nextid`
- [x] ported from v1: `tag`, `firewall`, `rename`, `destroy`

Still open:
- [ ] live bootstrap test on a disposable tmp VM
- [ ] `ui.confirm_destructive` wired into destructive paths
- [ ] optional: `backup` (vzdump wrapper)
- [ ] gum integration could go deeper (wizards for service/logs args)
