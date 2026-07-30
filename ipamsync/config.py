"""
ipamsync.config

Loads and validates the YAML config, and builds authenticated clients
from it.

## Credentials never live in this file

`config.yml` is committed; app tokens are not. Each side names either:

    token_env: PHPIPAM_SRC_TOKEN      # read from the environment
    token_command: "ansible-vault view ~/.ipam/src_token"   # or run this

`token_command` exists so any secret store works without this repo
growing a dependency on one -- ansible-vault, pass, gpg, a cloud secret
CLI. Its stdout is the token, stripped of trailing whitespace. Only the
side actually being used is resolved: the exporter never needs the
target's credentials, and the importer never needs the source's. That
matters here, because the two scripts run on *different machines*
separated by a one-way link -- neither host should hold both tokens.
"""

import os
import shlex
import subprocess

import yaml

DEFAULT_OPTIONS = {
    # See ipamsync.model.ADDRESS_OPTIONAL_FIELDS -- `tag` (on both
    # subnets and addresses) is an id into phpIPAM's ipTags table. True is
    # correct for two stock installs; set false if either side has custom
    # tags.
    "sync_tags": True,
    # Create a section on the target if the snapshot names one that does
    # not exist. Off by default: sections carry permissions, and silently
    # creating one that no group can see produces subnets nobody can
    # find. Better to have an operator create it deliberately.
    "create_missing_sections": False,

    # Delete target records the snapshot no longer contains, making the
    # replica a strict mirror instead of additive-only. Off by default:
    # with it off the worst a bad snapshot can do is add or update, and
    # that property is worth keeping until you actively want mirror
    # semantics. See "Deleting" in ipamsync.plan.
    "delete_drift": False,

    # Safety limit on the above. A run that would delete more than this
    # fraction of the in-scope target records is refused outright (with a
    # floor of ipamsync.plan.DELETE_LIMIT_FLOOR records, so small
    # datasets are not permanently blocked). Guards the case that
    # actually matters: an empty or mis-scoped snapshot arriving over a
    # one-way link and taking the whole replica with it.
    "delete_limit_fraction": 0.1,

    # Bypass that limit for one run. Set from --force-delete rather than
    # in the config file -- a standing "ignore the safety limit" is not a
    # policy anyone should be able to forget they set.
    "force_delete": False,
}


class ConfigError(Exception):
    """Raised for a missing, malformed, or incomplete config."""


def load_config(path):
    """Reads the YAML config, applies defaults, and validates enough of
    it to fail fast with a useful message rather than an AttributeError
    twenty lines into a sync."""
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
    else:
        sections = raw.get("sections")
        if not sections or not isinstance(sections, list):
            raise ConfigError(
                "Config needs either a non-empty `sections:` list (single "
                "source), e.g.\n  sections:\n    - Shared\n"
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
        "section_map": section_map,
        "sources": sources or [],
        "options": options,
    }


def resolve_sources(config, only=None):
    """Returns the master's subordinate list as [(name, source_config)].

    A master aggregating several airgapped subordinates configures them
    under `sources:`, each with its own snapshot directory and its own
    section on the master. Each entry is turned into a self-contained
    config that the existing single-source machinery can consume
    unchanged -- same target, same options, but that source's own
    `section_map`.

    `only` restricts the result to one named source.
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
    """Checks the fan-in invariants that keep one subordinate from
    destroying another's records.

    This is the single most important validation in the file. Each import
    treats everything in its target section that is absent from its
    snapshot as an orphan -- reportable as drift, and deletable once
    `delete_drift` is on. So if two subordinates ever share a target
    section, each run would see the other's subnets as orphans. With
    deletion enabled that is not a warning, it is one site silently
    wiping another's networks off the master on every sync.

    Refusing the config outright is the only safe response: there is no
    partially-correct way to run it.
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
        # Typed explicitly: an unquoted YAML `name: 123` arrives as an int
        # and used to reach the string formatting below as an
        # AttributeError, which is a poor way to learn your config has a
        # typo in it.
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
    """The name the given source section is known by on the target.
    Defaults to the same name; `section_map` overrides it, for when the
    two instances cannot agree on a section name."""
    return config["section_map"].get(source_section, source_section)


def _resolve_token(side_name, side):
    """Resolves one side's app token from `token_env` or `token_command`.

    The token is deliberately never accepted inline in the config file --
    the config is meant to be committed, and a token in a committed file
    is a token in the git history forever.
    """
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
            # stderr, not stdout -- stdout is the secret and must not be
            # echoed into logs on failure.
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
    """Builds a client for `source` or `target`. Imported lazily so that
    config validation and the unit tests do not require `requests`."""
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
