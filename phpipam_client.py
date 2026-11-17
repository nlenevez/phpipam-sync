#!/usr/bin/env python3
"""
phpipam_client.py

Generic client for phpIPAM's REST API: app-token authentication,
get/create/update/delete for subnets, addresses, L2 domains, VLANs and VRFs,
and section reads. Sync-specific behaviour lives in `ipamsync/client_ext.py`.

Endpoint shapes follow phpIPAM's API documentation and controller source
(https://github.com/phpipam/phpipam/blob/master/doc/API/api_documentation.md).
ipamsync confirms every create and delete by re-reading the record; if you
use this client directly, verify its writes against your own instance.

Authentication uses a phpIPAM API app in "app code" (static token) mode:
the token is sent in the "token" header on every request. There is no
login step and no expiry, so a 401 is a hard failure.

Supply the token directly (`token=`) or via an environment variable
(`token_env=`). Resolve secret-store tokens outside this module and pass
them via `token` (ipamsync's `token_command` does this).

Usage:
    from phpipam_client import PhpIpamClient

    client = PhpIpamClient(
        base_url="https://ipam.example.com", app_id="sync",
        token_env="PHPIPAM_TOKEN",
    )

    sections = client.get_sections()
    subnets = client.get_subnets_in_section(section_id=3)
    addresses = client.get_addresses_in_subnet(subnet_id=42)

    new_id = client.create_address(
        subnet_id=42, ip="10.0.0.5", hostname="router1", description="..."
    )
    client.update_address(address_id=new_id, description="updated")

Requires: requests
    pip install requests
"""

import os

import requests


class PhpIpamError(Exception):
    """Raised for a non-success API response or a connection failure.
    Carries `status_code` and the API's `api_code` where available."""

    def __init__(self, message, status_code=None, api_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


class PhpIpamClient:
    """Thin wrapper around phpIPAM's REST API, one instance per
    app_id/token pair."""

    def __init__(self, base_url, app_id, token=None, token_env=None,
                 verify_ssl=True, timeout=30):
        """
        base_url: e.g. "https://ipam.example.com" (no trailing slash,
            no /api/ suffix -- that's added automatically).
        app_id: the phpIPAM "API app" identifier (configured in phpIPAM's
            own Administration > API settings), not a phpIPAM username.
        token/token_env: the app code, given directly or as the name of an
            environment variable holding it.
        verify_ssl: set False only for self-signed lab instances; keep the
            URL https:// so the token is not sent in clear.
        """
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.verify_ssl = verify_ssl
        self.timeout = timeout

        if token is not None:
            self._token = token
        elif token_env is not None:
            self._token = os.environ.get(token_env)
            if self._token is None:
                raise PhpIpamError(
                    f"--token-env '{token_env}' is not set in the "
                    f"environment."
                )
        else:
            raise PhpIpamError(
                "Either token or token_env must be given."
            )

    # -- Core request plumbing ----------------------------------------

    def _request(self, method, path, params=None, json_body=None):
        """Issues one API request.

        `path` excludes the /api/{app_id}/ prefix, e.g. "subnets/42/".
        Returns the response's "data" field; raises PhpIpamError on any
        failure (a 401 is not retried).
        """
        url = f"{self.base_url}/api/{self.app_id}/{path.lstrip('/')}"
        headers = {
            "token": self._token,
            # Sent on every request, bodyless ones included: phpIPAM answers
            # an unrecognised Content-Type (as a proxy or php-fpm may
            # supply) with 415, and some versions refuse an empty one.
            "Content-Type": "application/json",
        }

        try:
            resp = requests.request(
                method, url, headers=headers, params=params,
                json=json_body, verify=self.verify_ssl, timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise PhpIpamError(f"Connection failed: {method} {url}: {e}")

        # Errors normally carry JSON; a proxy error page may not.
        try:
            body = resp.json()
        except ValueError:
            if resp.ok:
                return None
            raise PhpIpamError(
                f"{method} {path} failed with non-JSON response "
                f"(HTTP {resp.status_code}): {resp.text[:200]!r}",
                status_code=resp.status_code,
            )

        if resp.status_code == 401:
            raise PhpIpamError(
                f"{method} {path} returned 401 Unauthorized: "
                f"{body.get('message', 'token rejected')}. With static "
                f"app-code auth there's no login to retry -- check the "
                f"app code is correct and the phpIPAM app's security "
                f"mode is set to app-code (static token).",
                status_code=resp.status_code,
                api_code=body.get("code"),
            )

        if not resp.ok or not body.get("success", resp.ok):
            raise PhpIpamError(
                f"{method} {path} failed: {body.get('message', 'unknown error')}",
                status_code=resp.status_code,
                api_code=body.get("code"),
            )

        return body.get("data")

    # -- Sections ------------------------------------------------------

    def get_sections(self):
        """Returns all sections (a good connectivity/auth smoke test)."""
        return self._request("GET", "sections/") or []

    def get_section(self, section_id):
        return self._request("GET", f"sections/{section_id}/")

    # -- Subnets ---------------------------------------------------------

    def get_subnets_in_section(self, section_id):
        return self._request("GET", f"sections/{section_id}/subnets/") or []

    def get_subnet(self, subnet_id):
        return self._request("GET", f"subnets/{subnet_id}/")

    def get_addresses_in_subnet(self, subnet_id):
        return self._request("GET", f"subnets/{subnet_id}/addresses/") or []

    def create_subnet(self, *, subnet, mask, section_id, description=None,
                      **extra_fields):
        """Creates a subnet. `subnet` is the network address (e.g.
        "10.0.0.0"), `mask` the prefix length (e.g. 24). `extra_fields`
        are passed through verbatim."""
        payload = {
            "subnet": subnet,
            "mask": mask,
            "sectionId": section_id,
            **({"description": description} if description is not None else {}),
            **extra_fields,
        }
        return self._request("POST", "subnets/", json_body=payload)

    def update_subnet(self, subnet_id, **fields):
        """Partial update (PATCH) of the given fields."""
        return self._request("PATCH", f"subnets/{subnet_id}/", json_body=fields)

    def delete_subnet(self, subnet_id):
        return self._request("DELETE", f"subnets/{subnet_id}/")

    # -- Addresses ---------------------------------------------------------

    def get_address(self, address_id):
        return self._request("GET", f"addresses/{address_id}/")

    def search_address(self, ip):
        """Searches all subnets for an IP. Returns a list of matches."""
        return self._request("GET", f"addresses/search/{ip}/") or []

    def create_address(self, *, subnet_id, ip=None, hostname=None,
                       description=None, **extra_fields):
        """Creates an address at `ip`, or at the subnet's first free
        address if `ip` is omitted.

        Returns the new id (explicit ip) or the allocated IP (first_free).
        `extra_fields` are passed through verbatim."""
        if ip:
            payload = {
                "subnetId": subnet_id,
                "ip": ip,
                **({"hostname": hostname} if hostname is not None else {}),
                **({"description": description} if description is not None else {}),
                **extra_fields,
            }
            return self._request("POST", "addresses/", json_body=payload)
        else:
            payload = {
                **({"hostname": hostname} if hostname is not None else {}),
                **({"description": description} if description is not None else {}),
                **extra_fields,
            }
            return self._request(
                "POST", f"addresses/first_free/{subnet_id}/", json_body=payload
            )

    def update_address(self, address_id, **fields):
        """Partial update (PATCH) of the given fields."""
        return self._request("PATCH", f"addresses/{address_id}/", json_body=fields)

    def delete_address(self, address_id):
        return self._request("DELETE", f"addresses/{address_id}/")

    # -- L2 domains, VLANs and VRFs ---------------------------------------
    #
    # An L2 domain's section-id list is read as `sections` but written as
    # `permissions` (";"-separated). Domain 1 is implicitly in every
    # section. VLANs and VRFs are read back with their key as `id`.

    def get_l2domains(self):
        return self._request("GET", "l2domains/") or []

    def create_l2domain(self, name, *, sections=None, **fields):
        """Creates an L2 domain visible in the given section ids (sent as
        `permissions`)."""
        payload = {"name": name, **fields}
        if sections is not None:
            payload["permissions"] = ";".join(str(one) for one in sections)
        return self._request("POST", "l2domains/", json_body=payload)

    def update_l2domain(self, domain_id, *, sections=None, **fields):
        payload = dict(fields)
        if sections is not None:
            payload["permissions"] = ";".join(str(one) for one in sections)
        return self._request("PATCH", f"l2domains/{domain_id}/",
                             json_body=payload)

    def get_vlans(self):
        return self._request("GET", "vlans/") or []

    def get_vlan(self, vlan_id):
        return self._request("GET", f"vlans/{vlan_id}/")

    def create_vlan(self, *, number, name, domain_id, **fields):
        payload = {"number": number, "name": name,
                   "domainId": domain_id, **fields}
        return self._request("POST", "vlans/", json_body=payload)

    def update_vlan(self, vlan_id, **fields):
        return self._request("PATCH", f"vlans/{vlan_id}/", json_body=fields)

    def delete_vlan(self, vlan_id):
        return self._request("DELETE", f"vlans/{vlan_id}/")

    def get_vrfs(self):
        return self._request("GET", "vrfs/") or []

    def get_vrf(self, vrf_id):
        return self._request("GET", f"vrfs/{vrf_id}/")

    def create_vrf(self, *, name, sections=None, **fields):
        """Creates a VRF. Its section list is `sections` on read and write."""
        payload = {"name": name, **fields}
        if sections is not None:
            payload["sections"] = ";".join(str(one) for one in sections)
        return self._request("POST", "vrfs/", json_body=payload)

    def delete_vrf(self, vrf_id):
        return self._request("DELETE", f"vrfs/{vrf_id}/")

    def update_vrf(self, vrf_id, *, sections=None, **fields):
        payload = dict(fields)
        if sections is not None:
            payload["sections"] = ";".join(str(one) for one in sections)
        return self._request("PATCH", f"vrfs/{vrf_id}/", json_body=payload)
