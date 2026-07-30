"""
ipamsync.model

The field policy: which phpIPAM subnet/address fields are safe to carry
across two *independent* phpIPAM databases, and how to compare values
coming back from the API without generating false differences.

## Why an allowlist and not "copy everything"

Two separate phpIPAM installs share no primary keys. Every phpIPAM
record carries a mix of three kinds of column:

  1. Literal values   -- description, hostname, mac. Meaningful on their
                         own. Safe to copy verbatim.
  2. Local foreign keys -- sectionId, vlanId, vrfId, nameserverId,
                         deviceId, location, customer_id, scanAgent,
                         masterSubnetId, PTR. These are integers that
                         index *this instance's* other tables. Copying
                         them across instances silently points the record
                         at whatever unrelated row happens to hold that
                         id on the target -- the single most damaging
                         thing this tool could do. Never copied; the two
                         that matter (section, parent subnet) are
                         re-resolved by natural key at import time.
  3. Derived/local state -- editDate, lastScan, lastDiscovery, lastSeen,
                         isFull. Recomputed by the target instance;
                         copying them is noise at best.

So the lists below are a deliberately CONSERVATIVE allowlist of category
1 only. Anything not named is dropped.

## Honesty about schema drift

phpIPAM's column set varies across versions, and no list written from
documentation can be authoritative for *your* two instances. Rather than
pretend otherwise, the exporter reports every field it saw on the source
but does not carry (see `partition_subnet` / `partition_address`
returning `dropped`), so you can look at a real export and decide whether
anything you care about is missing. Add it to the allowlist if it is a
literal value; leave it out if it is a local id.

Custom fields (phpIPAM prefixes them `custom_`) are passed through
automatically -- they are user-defined literals by construction. The
target instance must have the same custom field defined, or its API will
reject the write; the importer surfaces that as a per-record error rather
than aborting the run.
"""

# -- Subnets -----------------------------------------------------------

#: Literal-value subnet fields carried across instances.
SUBNET_SYNCED_FIELDS = (
    "description",
    "showName",
    "allowRequests",
    "pingSubnet",
    "discoverSubnet",
    "resolveDNS",
    "DNSrecursive",
    "DNSrecords",
    "isFolder",
    "isPool",
    "threshold",
)

#: Subnet fields carried only when the controlling config option is on.
#: Subnets carry a `tag` for exactly the same reason addresses do (an id
#: into phpIPAM's `ipTags` table), so one option governs both.
SUBNET_OPTIONAL_FIELDS = {
    "sync_tags": ("tag",),
}

#: Subnet fields we know about and deliberately do NOT carry. Listed
#: explicitly so the "dropped unknown fields" report stays signal: a
#: field showing up there is genuinely unexpected, not just something we
#: already decided against.
SUBNET_EXCLUDED_FIELDS = frozenset({
    # identity / structural -- re-resolved by natural key at import
    "id", "subnet", "mask", "sectionId", "masterSubnetId",
    # local foreign keys
    "vlanId", "vrfId", "nameserverId", "scanAgent", "device", "deviceId",
    "location", "locationId", "customer_id", "permissions",
    "linked_subnet", "firewallAddressObject", "identifier", "domainId",
    # `gatewayId` points at an *address* id in this instance's own
    # ipaddresses table, and `gateway` is the object it resolves to.
    # Carrying either across would designate an unrelated address as the
    # gateway on the target. The gateway survives replication anyway: it
    # is the `is_gateway` flag on the address record itself, which IS
    # carried, and the target recomputes gatewayId from that.
    "gatewayId", "gateway",
    # derived / local state
    "editDate", "lastScan", "lastDiscovery", "isFull", "usage",
    "calculation", "subnetIsFull",
})

# -- Addresses ---------------------------------------------------------

#: Literal-value address fields carried across instances.
ADDRESS_SYNCED_FIELDS = (
    "hostname",
    "description",
    "mac",
    "owner",
    "note",
    "port",
    "is_gateway",
    "excludePing",
    "PTRignore",
)

#: Fields carried only when explicitly enabled in config, keyed by the
#: config option that controls them.
#:
#: `tag` is the one judgement call in this module: it is an integer id
#: into phpIPAM's `ipTags` table, i.e. technically a local foreign key.
#: In practice ids 1-4 are the stock Offline/Used/Reserved/DHCP tags
#: created identically by every phpIPAM install, so it transfers
#: correctly between two default installs. If either instance has custom
#: tags, set `options.sync_tags: false` in the config. The same option
#: governs the subnet-level `tag` -- see SUBNET_OPTIONAL_FIELDS.
ADDRESS_OPTIONAL_FIELDS = {
    "sync_tags": ("tag",),
}

ADDRESS_EXCLUDED_FIELDS = frozenset({
    # identity / structural
    "id", "ip", "subnetId", "ip_addr",
    # local foreign keys
    "deviceId", "device", "location", "locationId", "customer_id",
    "PTR", "firewallAddressObject", "nameserverId",
    # derived / local state
    "editDate", "lastSeen",
})

CUSTOM_FIELD_PREFIX = "custom_"

#: The key phpIPAM nests custom fields under when the API app has
#: "nest custom fields" enabled (`api.app_nest_custom_fields = 1`).
#:
#: Enabling it on the SOURCE app is strongly recommended, and is the only
#: reliable way to replicate custom fields. phpIPAM determines what is
#: custom by diffing the live table against its shipped SCHEMA.sql, so a
#: custom field's column name is whatever the admin typed -- a field
#: added through the UI as "Owner" is a column named `Owner`, with no
#: prefix of any kind. Confirmed on 1.8.1:
#:
#:     nesting off -> {... "Owner": "netops-team" ...}      (indistinguishable
#:                                                           from a standard field)
#:     nesting on  -> {... "custom_fields": {"Owner": "netops-team"} ...}
#:
#: With nesting on, phpIPAM tells us exactly which fields are custom and
#: no guessing is needed. With it off, this tool cannot safely tell a
#: custom field from a phpIPAM field it has not been taught about, and
#: carrying unknown fields blindly is precisely the mistake the allowlist
#: exists to prevent -- so they are reported as dropped instead.
#:
#: Note this is a READ-side format only: writes must send custom fields
#: flat. Posting a nested `custom_fields` object is rejected with
#: "Invalid request key custom_fields". The snapshot therefore stores
#: them flat, matching what has to go back on the wire.
#:
#: One further wrinkle, confirmed on 1.8.1: the nesting is NOT applied
#: consistently. A record whose custom fields are ALL null comes back
#: from a *list* endpoint with no `custom_fields` block at all, and the
#: raw columns leaked at top level instead:
#:
#:   GET sections/1/subnets/  (Owner set)  -> {... "custom_fields": {"Owner": "netops"} ...}
#:   GET sections/1/subnets/  (Owner null) -> {... "Owner": null ...}          # no block
#:   GET subnets/2000/        (Owner null) -> {... "custom_fields": {"Owner": null} ...}
#:
#: The single-record endpoint always nests, so it is the reliable place
#: to learn the custom-field *names*. The exporter probes one record per
#: table for exactly that and passes the names down as `custom_names`.
#: Two things follow: all-null records stop being misreported as unknown
#: fields (which would otherwise fire on every export and train you to
#: ignore the report), and a custom field CLEARED upstream now propagates
#: instead of being silently skipped.
NESTED_CUSTOM_FIELDS_KEY = "custom_fields"

#: Fields phpIPAM READS under one name but WRITES under another.
#:
#: Confirmed against phpIPAM 1.7 (2026-07-23). The subnets controller
#: validates incoming keys against the `subnets` *table columns*, where
#: the field is `state`, but serialises it on read as `tag`:
#:
#:     GET  subnets/12/  -> {... "tag": 2 ...}
#:     POST subnets/  {"tag": 2}    -> 400 "Invalid request key tag"
#:     POST subnets/  {"state": 2}  -> 201 Subnet created
#:
#: Addresses do NOT have this problem -- their controller accepts `tag`
#: and `state` alike -- so the aliasing is deliberately subnet-only
#: rather than applied globally.
#:
#: The snapshot always stores the READ name, so the file format stays
#: consistent with what the API reports and is not contaminated by one
#: instance's write quirks. Translation happens at the point of writing.
SUBNET_WRITE_ALIASES = {"tag": "state"}


def to_subnet_write_fields(fields):
    """Renames subnet fields to the names phpIPAM accepts on write."""
    return {SUBNET_WRITE_ALIASES.get(name, name): value
            for name, value in fields.items()}


def flatten_custom_fields(raw):
    """Lifts a record's nested `custom_fields` block up to the top level.

    The snapshot stores custom fields flat (writes require that), so a
    target record read back with nesting enabled must be flattened before
    it can be compared against the snapshot. Skipping this is not a
    cosmetic problem: the comparison would look for a flat `Owner`,
    never find it, and rewrite every custom field on every single run --
    an endless update loop that would churn the target's change history
    forever and bury real changes in noise.

    Returns the record unchanged when there is no nested block, so it is
    safe to apply to every record regardless of the API app's setting.
    """
    nested = raw.get(NESTED_CUSTOM_FIELDS_KEY)
    if not isinstance(nested, dict):
        return raw
    flat = {name: value for name, value in raw.items()
            if name != NESTED_CUSTOM_FIELDS_KEY}
    flat.update(nested)
    return flat


def normalise(value):
    """Collapses a field value to a comparable string.

    phpIPAM's API is loosely typed: the same logical field comes back as
    `1` or `"1"` or `true` depending on version and endpoint, and "unset"
    is variously `None`, `""`, or absent. Comparing raw values across two
    instances therefore reports differences that do not exist, and the
    importer would rewrite every field on every run -- churn that would
    make a genuine change impossible to spot in the logs.

    Normalising both sides through here makes comparison stable:
    None and "" are the same thing (unset), and booleans and numbers
    compare as their string form.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _partition(raw, synced_fields, excluded_fields, custom_names=()):
    """Splits one API record into (carried, dropped).

    `carried` holds the allowlisted literal fields plus this record's
    custom fields, normalised. `dropped` names the fields present on the
    record that are neither carried nor in the known-excluded set -- the
    schema drift report described in the module docstring.

    Custom fields are recognised two ways, in order of trustworthiness:

      1. phpIPAM's own `custom_fields` block, present when the API app
         has "nest custom fields" enabled. This is authoritative --
         phpIPAM decides what is custom by diffing the live table against
         its shipped SCHEMA.sql -- so it works whatever the fields are
         named. Strongly preferred; see NESTED_CUSTOM_FIELDS_KEY.

      2. Failing that, the `custom_*` naming convention. This is only a
         heuristic: phpIPAM does NOT require the prefix, and a field
         added through its UI as "Owner" is a column literally named
         `Owner`. Confirmed on 1.8.1 -- such a field is invisible to this
         fallback and lands in `dropped` instead, which is the report
         telling you to turn nesting on.
    """
    carried = {}
    for field in synced_fields:
        if field in raw:
            carried[field] = normalise(raw[field])

    nested = raw.get(NESTED_CUSTOM_FIELDS_KEY)
    if isinstance(nested, dict):
        for field, value in nested.items():
            carried[field] = normalise(value)

    for field, value in raw.items():
        # NESTED_CUSTOM_FIELDS_KEY is itself "custom_fields", so it
        # matches the prefix below. It is the *container*, already
        # unpacked above -- carrying it too would stringify the whole
        # dict into a field of its own and phpIPAM rejects the write with
        # "Invalid request key custom_fields".
        if field == NESTED_CUSTOM_FIELDS_KEY:
            continue
        if field.startswith(CUSTOM_FIELD_PREFIX) or field in custom_names:
            carried[field] = normalise(value)

    known = (set(synced_fields) | set(excluded_fields)
             | {NESTED_CUSTOM_FIELDS_KEY} | set(carried))
    dropped = sorted(
        field for field in raw
        if field not in known and not field.startswith(CUSTOM_FIELD_PREFIX)
    )
    return carried, dropped


def _apply_options(base_fields, optional_fields, options):
    """Resolves the option-controlled fields into (synced, suppressed).

    A field left out by an option is treated as *excluded*, not dropped --
    leaving it out was a deliberate configuration choice, so it must not
    show up in the schema-drift report and look like an oversight.
    """
    synced = list(base_fields)
    suppressed = set()
    for option, fields in optional_fields.items():
        if options.get(option, True):
            synced.extend(fields)
        else:
            suppressed.update(fields)
    return tuple(synced), suppressed


def partition_subnet(raw, options=None, custom_names=()):
    """Splits a phpIPAM subnet record into (carried_fields, dropped_names).

    `custom_names` is the set of custom-field names discovered for this
    table (see NESTED_CUSTOM_FIELDS_KEY). Supplying it lets a record whose
    custom fields are all null -- where phpIPAM's list endpoint leaks the
    raw columns at top level instead of nesting them -- still be handled
    as custom rather than reported as an unknown field.
    """
    synced, suppressed = _apply_options(
        SUBNET_SYNCED_FIELDS, SUBNET_OPTIONAL_FIELDS, options or {}
    )
    return _partition(raw, synced, SUBNET_EXCLUDED_FIELDS | suppressed,
                      custom_names)


def partition_address(raw, options=None, custom_names=()):
    """Splits a phpIPAM address record into (carried_fields, dropped_names).

    `options` is the config's `options` mapping; it decides whether the
    conditionally-synced fields in ADDRESS_OPTIONAL_FIELDS are carried.
    `custom_names` is as for partition_subnet().
    """
    synced, suppressed = _apply_options(
        ADDRESS_SYNCED_FIELDS, ADDRESS_OPTIONAL_FIELDS, options or {}
    )
    return _partition(raw, synced, ADDRESS_EXCLUDED_FIELDS | suppressed,
                      custom_names)


def diff_fields(desired, current):
    """Returns the subset of `desired` whose value differs from `current`.

    Both sides are normalised first. An empty result means the target
    record already matches the snapshot and needs no write at all --
    which is the common case on a steady-state sync, and the reason the
    importer can run frequently without generating pointless API traffic
    or edit-history noise on the target instance.

    Note the asymmetry: a field present on the target but absent from
    `desired` is NOT reported. The snapshot describes what the source
    knows about; it does not assert that every other field is empty.
    """
    changed = {}
    for field, want in desired.items():
        if normalise(want) != normalise(current.get(field)):
            changed[field] = want
    return changed
