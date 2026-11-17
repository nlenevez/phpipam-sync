# Exporting phpIPAM to git

The exporter on its own, as a history and backup of phpIPAM. Each
instance gets a **data repo** holding its configuration and snapshot,
with this repo as a submodule at `tool/`. A scheduled run commits only
when something changed, so `git log -p` is a per-subnet change history.

```
<site>-ipam/                 data repo, one per phpIPAM instance
  phpipam-sync.yml           source URL, API app, sections (no token)
  .gitignore
  tool/                      submodule -> phpipam-sync, pinned to a commit
  snapshot/
    manifest.json            counts, sections, checksums
    sections/<section>/      _section.json (VLANs, VRFs, folders) and one
                             file per subnet with all its addresses
```

The snapshot is the importer's format, so a data repo can also feed a
replica or a fan-in master: point `snapshot_dir` at `<clone>/snapshot`.

No pip needed: `requests` and `PyYAML` are vendored in `ipamsync/_vendor`
and used when not installed. Python 3.9+; on RHEL 8 that means
`PYTHON=/usr/bin/python3.12`.

## Scope

In `phpipam-sync.yml`:

```yaml
sections: all          # every section the API app can see
# or
sections:
  - Networks           # only these
```

A section export carries subnets, addresses, custom fields, folders,
VLANs and VRFs (README, "What is and isn't replicated"). Devices,
locations, nameservers and users are not exported.

A section the API app has no permission on is invisible to it, and so
silently absent from `sections: all`. Check the manifest's `sections`
after the first run.

For separate histories per section, give each its own data repo.
Removing a section from `sections:` deletes its files in the next
commit.

## Setup

1. **API app** on the instance (Administration > API): app id e.g.
   `export`, **Read**, **SSL with App code**, **Nest custom fields = Yes**.
2. **Data repo**: create an empty `<site>-ipam` repo next to
   `phpipam-sync` on your git server (same group or project), then:

   ```sh
   git clone <ssh URL of site-a-ipam> /srv/ipam/site-a && cd /srv/ipam/site-a
   git switch -c main
   git submodule add ../phpipam-sync.git tool
   cp tool/contrib/data-repo/phpipam-sync.yml phpipam-sync.yml     # edit
   cp tool/contrib/data-repo/gitignore .gitignore
   ```
3. **First export by hand**, read before committing:

   ```sh
   read -rs PHPIPAM_SRC_TOKEN && export PHPIPAM_SRC_TOKEN
   /usr/bin/python3.12 tool/ipam_export.py --config phpipam-sync.yml --out-dir snapshot
   cat snapshot/manifest.json        # sections, subnet and address counts
   git add -A && git commit -m "first export" && git push -u origin main
   ```

   Check the reported `dropped_source_fields` (DEPLOYMENT.md 1.5).

## Scheduling

Both options run `tool/contrib/export.sh`: fast-forward from the remote,
export into `snapshot/`, commit only if something changed, push. A lock
stops overlapping runs. The last line is `result: ok (exit 0)` or
`result: FAILED (exit N)`.

### Cron

`/etc/ipam-export/site-a.env` (mode 0600 or 0640, outside the repo):

```sh
PHPIPAM_SRC_TOKEN='...'
PYTHON=/usr/bin/python3.12
GIT_SSH_COMMAND="ssh -i /home/ipam-export/.ssh/ipam_export -o BatchMode=yes"
```

```
*/30 * * * * cd /srv/ipam/site-a && tool/contrib/export.sh /etc/ipam-export/site-a.env >>/var/log/ipam-export/site-a.log 2>&1
```

### Ansible with a vault

```sh
cp tool/contrib/ansible/phpipam-export.yml tool/contrib/ansible/inventory.example.yml ~/ipam-ansible/
cd ~/ipam-ansible && mv inventory.example.yml inventory.yml        # edit
ansible-vault create vault.yml      # layout: tool/contrib/ansible/vault.example.yml
ansible-playbook -i inventory.yml phpipam-export.yml --vault-password-file ~/.ipam-vault-pass
```

`vault_phpipam_tokens` holds one API token per instance name, or a
`default`. Each token reaches the script in a mode-0600 file in a
private temporary directory, deleted afterwards, so it never appears in
`-vvv` output or `ps`. Every instance is attempted; the play fails at
the end naming any that failed. Tested with ansible-core 2.16 (RHEL
8.10).

### Watch it

Alert on a non-`ok` result line, or on a log not written for longer than
the schedule. A quiet period makes no commit, so the last commit's age
is not a health signal.

## Updating the tool

```sh
cd /srv/ipam/site-a
git -C tool fetch origin && git -C tool checkout origin/main
git add tool && git commit -m "tool: update" && git push
```

## Restoring

To rebuild sections on a fresh instance, point an importer config's
`target` at it and run `ipam_import.py --snapshot-dir snapshot` without
`--apply` first to read the plan. The sections and custom fields must
exist on the target first (DEPLOYMENT.md 2.1, 2.2).
