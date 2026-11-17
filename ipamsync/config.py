"""
ipamsync.config

Loads and validates the YAML config, and builds authenticated clients
from it.

Tokens are never stored in the (committed) config. Each side names either:

    token_env: PHPIPAM_SRC_TOKEN      # read from the environment
    token_command: "ansible-vault view ~/.ipam/src_token"   # or run this

`token_command` stdout is the token, trailing whitespace stripped. Only the
side in use is resolved, so neither host needs both tokens.
"""

import os
import shlex
import subprocess

import yaml

DEFAULT_OPTIONS = {
    # `tag` is an id into phpIPAM's ipTags table; set false if either side
    # has custom tags. See ipamsync.model.ADDRESS_OPTIONAL_FIELDS.
    "sync_tags": True,
    # Create missing target sections. Off by default: sections carry
    # permissions, and one no group can see hides its subnets.
    "create_missing_sections": False,

    # Delete target records absent from the snapshot (strict mirror).
    # Off by default: additive-only. See "Deleting" in ipamsync.plan.
    "delete_drift": False,

    # Refuse a run deleting more than this fraction of in-scope target
    # records (floor: ipamsync.plan.DELETE_LIMIT_FLOOR). Guards against an
    # empty or mis-scoped snapshot wiping the replica.
    "delete_limit_fraction": 0.1,

    # Bypass the limit for one run. Set by --force-delete, not the config.
    "force_delete": False,
}


class ConfigError(Exception):
    """Raised for a missing, malformed, or incomplete config."""


def load_config(path):
    """Reads the YAML config, applies defaults, and validates it."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError:
        raise ConfigError(
            f"Config {path} not found. Copy config.example.yml to "
            f"{path} and fill in your two instances."
        )
    except yaml.YAMLError as exc:
        raise ConfigError(f"Config {path} is not valid YAML: {exc}")

    if not isinstance(raw, dict):
        raise ConfigError(f"Config {path} must be a YAML mapping at the top level.")

    sources = raw.get("sources")
    if sources is not None:
        # Master (fan-in) config: several subordinates, one target.
        _validate_sources(sources)
        sections = []
    elif isinstance(raw.get("sections"), str) and raw["sections"].strip().casefold() == "all":
        # Every section the API app can see; expanded by the exporter.
        sections = []
    else:
        sections = raw.get("sections")
        if not sections or not isinstance(sections, list):
            raise ConfigError(
                "Config needs either a non-empty `sections:` list (single "
                "source), e.g.\n  sections:\n    - Shared\n"
                "or `sections: all` (export every section), "
                "or a `sources:` list (a master aggregating several "
                "subordinates) -- see config.master.example.yml."
            )
        if not all(isinstance(name, str) and name.strip() for name in sections):
            raise ConfigError(
                "Every entry under `sections:` must be a section name string.")

    section_map = raw.get("section_map") or {}
    if not isinstance(section_map, dict):
        raise ConfigError(
            "`section_map:` must be a mapping of source section name -> "
            "target section name, e.g.\n  section_map:\n    Shared: Shared-from-1"
        )

    options = dict(DEFAULT_OPTIONS)
    supplied_options = raw.get("options") or {}
    if not isinstance(supplied_options, dict):
        raise ConfigError("`options:` must be a mapping.")
    unknown = set(supplied_options) - set(DEFAULT_OPTIONS)
    if unknown:
        raise ConfigError(
            f"Unknown option(s) {sorted(unknown)}. Known options: "
            f"{sorted(DEFAULT_OPTIONS)}. (A typo'd option would otherwise "
            f"be silently ignored and the default used instead.)"
        )
    options.update(supplied_options)

    return {
        "source": raw.get("source") or {},
        "target": raw.get("target") or {},
        "sections": [name.strip() for name in sections],
        "all_sections": sources is None and not sections,
        "section_map": section_map,
        "sources": sources or [],
        "options": options,
    }


def resolve_sources(config, only=None):
    """Returns the master's `sources:` as [(name, source_config)].

    Each entry becomes a self-contained single-source config: shared target
    and options, its own sections and `section_map`. `only` restricts the
    result to one named source.
    """
    sources = config.get("sources") or []
    if not sources:
        return []

    if only is not None:
        matching = [s for s in sources if s["name"] == only]
        if not matching:
            raise ConfigError(
                f"No source named {only!r}. Configured sources: "
                f"{', '.join(s['name'] for s in sources)}"
            )
        sources = matching

    resolved = []
    for source in sources:
        resolved.append((source["name"], {
            "target": config["target"],
            "sections": source.get("sections") or [],
            "section_map": source.get("section_map") or {},
            "options": config["options"],
            "snapshot_dir": source["snapshot_dir"],
        }))
    return resolved


def _validate_sources(sources):
    """Checks the fan-in invariants: unique names, snapshot dirs and target
    sections per source.

    Each import treats records in its target section that are absent from
    its snapshot as drift (deleted with `delete_drift`), so two sources
    sharing a section would delete each other's records.
    """
    if not isinstance(sources, list):
        raise ConfigError("`sources:` must be a list of subordinate definitions.")

    seen_names, seen_dirs, section_owner = set(), {}, {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ConfigError(f"`sources[{index}]` must be a mapping.")
        name = source.get("name")
        if not name:
            raise ConfigError(f"`sources[{index}]` is missing `name`.")
        # Unquoted YAML `name: 123` arrives as an int.
        if not isinstance(name, str):
            raise ConfigError(
                f"`sources[{index}].name` must be a string, got "
                f"{type(name).__name__} ({name!r}). Quote it if it looks "
                f"like a number."
            )
        if not source.get("snapshot_dir"):
            raise ConfigError(f"source {name!r} is missing `snapshot_dir`.")
        if not isinstance(source["snapshot_dir"], str):
            raise ConfigError(
                f"source {name!r}: `snapshot_dir` must be a string path, got "
                f"{type(source['snapshot_dir']).__name__}."
            )

        if name in seen_names:
            raise ConfigError(f"Two sources are both named {name!r}.")
        seen_names.add(name)

        snapshot_dir = str(source["snapshot_dir"]).rstrip("/")
        if snapshot_dir in seen_dirs:
            raise ConfigError(
                f"Sources {seen_dirs[snapshot_dir]!r} and {name!r} both read "
                f"{snapshot_dir!r}. Each subordinate needs its own mirrored "
                f"repo, or they will overwrite each other's records."
            )
        seen_dirs[snapshot_dir] = name

        section_map = source.get("section_map") or {}
        if not isinstance(section_map, dict):
            raise ConfigError(f"source {name!r}: `section_map` must be a mapping.")

        # Every section this source can write to on the target.
        targets = set(section_map.values())
        for section in source.get("sections") or []:
            targets.add(section_map.get(section, section))
        if not targets:
            raise ConfigError(
                f"source {name!r} declares no sections. Set `sections:` to the "
                f"section name(s) as exported by that subordinate, and "
                f"`section_map:` to the section they land in on this master."
            )

        for section in targets:
            owner = section_owner.get(section.casefold())
            if owner and owner != name:
                raise ConfigError(
                    f"Sources {owner!r} and {name!r} both target the section "
                    f"{section!r} on this master.\n"
                    f"Each subordinate must own its own section. Sharing one "
                    f"means every import sees the other's subnets as records "
                    f"the snapshot no longer contains -- reported as drift, "
                    f"and DELETED outright once options.delete_drift is on.\n"
                    f"Give each source a distinct section via `section_map`, "
                    f"e.g.\n"
                    f"  section_map: {{<their section>: {name.title()}}}"
                )
            section_owner[section.casefold()] = name


def target_section_name(config, source_section):
    """The target name for a source section: its `section_map` entry, else
    the same name."""
    return config["section_map"].get(source_section, source_section)


def _resolve_token(side_name, side):
    """Resolves one side's app token from `token_env` or `token_command`
    (never inline: the config is committed)."""
    token_env = side.get("token_env")
    token_command = side.get("token_command")

    if token_env and token_command:
        raise ConfigError(
            f"`{side_name}` sets both token_env and token_command -- pick one."
        )

    if token_env:
        token = os.environ.get(token_env)
        if not token:
            raise ConfigError(
                f"`{side_name}.token_env` names ${token_env}, but that "
                f"variable is unset or empty in this environment."
            )
        return token

    if token_command:
        try:
            completed = subprocess.run(
                shlex.split(token_command),
                capture_output=True, text=True, check=True,
            )
        except FileNotFoundError as exc:
            raise ConfigError(
                f"`{side_name}.token_command` could not be run: {exc}"
            )
        except subprocess.CalledProcessError as exc:
            # stderr only: stdout is the secret.
            raise ConfigError(
                f"`{side_name}.token_command` exited {exc.returncode}: "
                f"{(exc.stderr or '').strip()[:300]}"
            )
        token = completed.stdout.strip()
        if not token:
            raise ConfigError(
                f"`{side_name}.token_command` produced no output -- expected "
                f"the phpIPAM app token on stdout."
            )
        return token

    raise ConfigError(
        f"`{side_name}` must set either token_env or token_command. "
        f"Tokens are never read from the config file itself (it is meant "
        f"to be committed)."
    )


def build_client(config, side_name):
    """Builds a client for `source` or `target`. Imported lazily so config
    validation and tests do not need `requests`."""
    from ipamsync.client_ext import SyncPhpIpamClient

    side = config.get(side_name) or {}
    for required in ("base_url", "app_id"):
        if not side.get(required):
            raise ConfigError(f"`{side_name}` is missing required key `{required}`.")

    return SyncPhpIpamClient(
        base_url=side["base_url"],
        app_id=side["app_id"],
        token=_resolve_token(side_name, side),
        verify_ssl=side.get("verify_ssl", True),
        timeout=side.get("timeout", 30),
    )
