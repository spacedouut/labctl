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
- [ ] optional: `backup` (vzdump wrapper)
- [ ] gum integration could go deeper (wizards for service/logs args)
- [ ] template regeneration: tpl-ubuntu-26 still has the networkd-wait-online
      boot hang (provision recovers it, but the template itself should be
      rebuilt from a bootstrapped VM: clone 9001 -> provision -> snapshot)
- [ ] ssh known_hosts staleness on reused DHCP IPs (maybe ssh-keygen -R on
      destroy, or a --no-key-check style flag for service/exec/logs)
