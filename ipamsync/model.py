"""
ipamsync.model

Field policy: which phpIPAM fields are carried between two independent
instances, and how values are compared.

Only literal values (description, hostname, mac, ...) are carried, via an
allowlist. Local foreign keys (sectionId, vlanId, masterSubnetId, ...) are
instance-local ids and are never copied; section and parent subnet are
re-resolved by natural key on import. Derived state (editDate, lastSeen,
...) is left to the target.

Fields seen but neither carried nor known-excluded are reported as
`dropped`, so schema differences show up in an export. Custom fields pass
through; the target must define the same custom fields.
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
#: `tag` shares its option with addresses (see ADDRESS_OPTIONAL_FIELDS).
SUBNET_OPTIONAL_FIELDS = {
    "sync_tags": ("tag",),
}

#: Known subnet fields deliberately not carried (kept out of the dropped
#: report).
SUBNET_EXCLUDED_FIELDS = frozenset({
    # identity / structural -- re-resolved by natural key at import
    "id", "subnet", "mask", "sectionId", "masterSubnetId",
    # local foreign keys
    "vlanId", "vrfId", "nameserverId", "scanAgent", "device", "deviceId",
    "location", "locationId", "customer_id", "permissions",
    "linked_subnet", "firewallAddressObject", "identifier", "domainId",
    # local address id; the gateway travels as the address's `is_gateway`
    "gatewayId", "gateway",
    # derived / local state
    "editDate", "lastScan", "lastDiscovery", "isFull", "usage",
    "calculation", "subnetIsFull",
})

# -- L2 domains, VLANs and VRFs ----------------------------------------
#
# Replicated as records (unattached ones included), matched by natural key:
# a VLAN is (L2 domain name, number), a VRF is (target section, name).

#: Literal-value L2 domain fields carried across instances.
DOMAIN_SYNCED_FIELDS = ("name", "description")

#: `permissions` is a ";"-separated list of local section ids; the importer
#: rebuilds it from the target section.
DOMAIN_EXCLUDED_FIELDS = frozenset({
    "id", "permissions", "sections", "editDate", "customer_id",
})

#: Literal-value VLAN fields carried across instances.
VLAN_SYNCED_FIELDS = ("name", "number", "description")

#: `domainId` is a local id; the domain travels by name instead.
VLAN_EXCLUDED_FIELDS = frozenset({
    "id", "vlanId", "domainId", "editDate", "customer_id",
})

#: Literal-value VRF fields carried across instances.
VRF_SYNCED_FIELDS = ("name", "rd", "description")

#: `sections` is a local id list, rebuilt on the target.
VRF_EXCLUDED_FIELDS = frozenset({
    "id", "vrfId", "sections", "editDate", "customer_id",
})


def partition_domain(raw, options=None, custom_names=()):
    return _partition(raw, DOMAIN_SYNCED_FIELDS, DOMAIN_EXCLUDED_FIELDS,
                      custom_names)


def partition_vlan(raw, options=None, custom_names=()):
    return _partition(raw, VLAN_SYNCED_FIELDS, VLAN_EXCLUDED_FIELDS,
                      custom_names)


def partition_vrf(raw, options=None, custom_names=()):
    return _partition(raw, VRF_SYNCED_FIELDS, VRF_EXCLUDED_FIELDS,
                      custom_names)


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

#: Fields carried only when their config option is on.
#:
#: `tag` is an id into phpIPAM's `ipTags` table. Ids 1-4 are the stock
#: tags on every install; with custom tags, set `options.sync_tags: false`.
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

#: Key phpIPAM nests custom fields under when the API app has "Nest custom
#: fields" enabled. Needed on the source app: custom columns carry no
#: prefix, so without nesting they are indistinguishable from unknown
#: fields and are reported as dropped.
#:
#: Read-side only: writes must send custom fields flat ("Invalid request key
#: custom_fields" otherwise), so the snapshot stores them flat.
#:
#: List endpoints omit the block when all of a record's custom fields are
#: null; single-record GETs always nest. The exporter probes one record per
#: table for the names and passes them down as `custom_names`.
NESTED_CUSTOM_FIELDS_KEY = "custom_fields"

#: Fields phpIPAM reads under one name but writes under another. Subnets
#: read as `tag` but are written as `state` ("Invalid request key tag");
#: addresses accept both. The snapshot stores the read name.
SUBNET_WRITE_ALIASES = {"tag": "state"}


def to_subnet_write_fields(fields):
    """Renames subnet fields to the names phpIPAM accepts on write."""
    return {SUBNET_WRITE_ALIASES.get(name, name): value
            for name, value in fields.items()}


def flatten_custom_fields(raw):
    """Lifts a record's nested `custom_fields` block to the top level, to
    compare against the flat snapshot. No-op without a nested block."""
    nested = raw.get(NESTED_CUSTOM_FIELDS_KEY)
    if not isinstance(nested, dict):
        return raw
    flat = {name: value for name, value in raw.items()
            if name != NESTED_CUSTOM_FIELDS_KEY}
    flat.update(nested)
    return flat


def normalise(value):
    """Collapses a field value to a comparable string: None == "", and
    booleans/numbers compare as strings (phpIPAM's API is loosely typed)."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _partition(raw, synced_fields, excluded_fields, custom_names=()):
    """Splits one API record into (carried, dropped).

    `carried`: allowlisted fields plus custom fields, normalised. `dropped`:
    fields neither carried nor known-excluded. Custom fields come from the
    nested block, `custom_names`, or (heuristically) the `custom_` prefix.
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
        # The container itself matches the prefix; already unpacked above.
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
    Suppressed fields count as excluded, not dropped."""
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

    `custom_names`: custom-field names discovered for this table (see
    NESTED_CUSTOM_FIELDS_KEY).
    """
    synced, suppressed = _apply_options(
        SUBNET_SYNCED_FIELDS, SUBNET_OPTIONAL_FIELDS, options or {}
    )
    return _partition(raw, synced, SUBNET_EXCLUDED_FIELDS | suppressed,
                      custom_names)


def partition_address(raw, options=None, custom_names=()):
    """Splits a phpIPAM address record into (carried_fields, dropped_names).
    `options` and `custom_names` as for partition_subnet()."""
    synced, suppressed = _apply_options(
        ADDRESS_SYNCED_FIELDS, ADDRESS_OPTIONAL_FIELDS, options or {}
    )
    return _partition(raw, synced, ADDRESS_EXCLUDED_FIELDS | suppressed,
                      custom_names)


def diff_fields(desired, current):
    """Returns the subset of `desired` whose normalised value differs from
    `current`. Fields only on `current` are ignored."""
    changed = {}
    for field, want in desired.items():
        if normalise(want) != normalise(current.get(field)):
            changed[field] = want
    return changed
