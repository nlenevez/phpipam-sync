"""
ipamsync.client_ext

The one thing the vendored `phpipam_client.py` does not provide.

`phpipam_client.py` is a generic phpIPAM API client that knows nothing
about replication, and it is kept that way deliberately: anything this
tool needs beyond it lives in a subclass here rather than as a local edit
to that file, so the client stays independently reusable and the
sync-specific behaviour stays in one place.

Sections are read-only upstream (get_sections / get_section) because no
workflow there ever created one. This tool can, but only when
`options.create_missing_sections` is enabled -- see ipamsync.config for
why that is off by default.

## Empty collections are an error in phpIPAM, not an empty list

Confirmed against phpIPAM 1.8.1 and 1.7.4 (docker
`phpipam/phpipam-www`): a collection endpoint with nothing in it returns
**HTTP 404 with a "No ... found" message**, not `200` with `[]`:

    GET /api/sync/subnets/11/addresses/  -> 404 {"message": "No addresses found"}
    GET /api/sync/sections/500/subnets/  -> 404 {"message": "No subnets found"}

The vendored client surfaces any non-success response as a PhpIpamError,
which is right in general but wrong for these: a subnet with no addresses
is completely ordinary (every supernet has none), and an empty target
section is the normal state of a *first* import. Left unhandled, the very
first run of this tool would fail on both counts.

`_read_collection` below maps that specific case to an empty list. It
matches on the "No ... found" message rather than on the 404 alone, so a
genuine "that subnet does not exist" 404 still raises -- silently
treating a missing subnet as an empty one would make the importer create
records against a subnet that was never there.
"""

from phpipam_client import PhpIpamClient, PhpIpamError


class SyncPhpIpamClient(PhpIpamClient):
    """PhpIpamClient plus section creation and empty-collection handling."""

    @staticmethod
    def _is_empty_collection(error):
        """True for phpIPAM's "this collection is empty" 404, false for
        every other 404 (bad id, unknown endpoint, no permission)."""
        if error.status_code != 404:
            return False
        message = str(error).lower()
        return "no " in message and " found" in message

    def _read_collection(self, method, *args, **kwargs):
        """Runs a collection read, returning [] where phpIPAM reports the
        collection as empty rather than raising."""
        try:
            return method(*args, **kwargs) or []
        except PhpIpamError as exc:
            if self._is_empty_collection(exc):
                return []
            raise

    def get_sections(self):
        return self._read_collection(super().get_sections)

    def get_subnets_in_section(self, section_id):
        return self._read_collection(super().get_subnets_in_section, section_id)

    def get_addresses_in_subnet(self, subnet_id):
        return self._read_collection(super().get_addresses_in_subnet, subnet_id)

    def create_section(self, name, **fields):
        """Creates a section and returns whatever phpIPAM reports as the
        new id.

        Like every other write in the vendored client, this endpoint's
        exact response shape is taken from phpIPAM's API documentation
        and has not been confirmed against a live instance -- so callers
        should treat the return value as a hint and confirm the section
        exists by re-reading it by name. ipamsync.target.resolve_created()
        does exactly that.
        """
        payload = {"name": name, **fields}
        return self._request("POST", "sections/", json_body=payload)
