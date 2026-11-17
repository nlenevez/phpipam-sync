"""
ipamsync.client_ext

Sync-specific additions to the generic `phpipam_client.py`: section
creation and empty-collection handling.

phpIPAM returns HTTP 404 with a "No ... found" message for an empty
collection, not 200 with `[]`:

    GET /api/sync/subnets/11/addresses/  -> 404 {"message": "No addresses found"}
    GET /api/sync/sections/500/subnets/  -> 404 {"message": "No subnets found"}

`_read_collection` maps that case to an empty list. It matches the message,
not the 404 alone, so "that subnet does not exist" still raises.
"""

from phpipam_client import PhpIpamClient, PhpIpamError


class SyncPhpIpamClient(PhpIpamClient):
    """PhpIpamClient plus section creation and empty-collection handling."""

    #: Endings of phpIPAM's empty-collection message: subnets/addresses say
    #: "No ... found", VLANs/VRFs say "No ... configured".
    EMPTY_COLLECTION_ENDINGS = ("found", "configured")

    @classmethod
    def _is_empty_collection(cls, error):
        """True for phpIPAM's empty-collection 404, false for any other 404
        (bad id, unknown endpoint, no permission)."""
        if error.status_code != 404:
            return False
        message = str(error).lower()
        if "no " not in message:
            return False
        return any(
            message.rstrip(". ").endswith(ending)
            for ending in cls.EMPTY_COLLECTION_ENDINGS
        )

    def _read_collection(self, method, *args, **kwargs):
        """Runs a collection read, returning [] for an empty collection."""
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

    # No VLANs/VRFs is normal (e.g. a master before its first import).
    def get_l2domains(self):
        return self._read_collection(super().get_l2domains)

    def get_vlans(self):
        return self._read_collection(super().get_vlans)

    def get_vrfs(self):
        return self._read_collection(super().get_vrfs)

    def create_section(self, name, **fields):
        """Creates a section and returns what phpIPAM reports as the new id.

        Treat the return value as a hint; confirm by re-reading by name
        (ipamsync.target.resolve_created()).
        """
        payload = {"name": name, **fields}
        return self._request("POST", "sections/", json_body=payload)
