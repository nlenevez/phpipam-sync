#!/usr/bin/env python3
"""
phpipam_client.py

Generic, reusable client for phpIPAM's REST API: per-app token
authentication, and get/create/update for subnets and addresses
(phpIPAM's two most commonly automated entities), plus read access to
sections and VLANs. It knows nothing about replication -- everything
specific to this tool lives in `ipamsync/`, and the empty-collection and
section-creation behaviour it lacks is added by subclass in
`ipamsync/client_ext.py` rather than by editing this file.

IMPORTANT -- confirmation status: every endpoint shape used here (auth
header, required fields for create/update, response envelope) comes from
phpIPAM's own published API documentation and controller source
(https://github.com/phpipam/phpipam/blob/master/doc/API/api_documentation.md,
https://github.com/phpipam/phpipam/blob/master/api/controllers/Addresses.php,
.../Subnets.php) -- it was NOT written against a live instance. The
replication code above it does not take those shapes on trust: every
create and delete is confirmed by re-reading the record by natural key
(see ipamsync/target.py), which is what turned up the quirks documented
in the README. If you use this client directly, treat its write methods
as unverified until you have exercised them against your own instance.
get_sections() is the safest first call to smoke-test connectivity/auth.

Authentication (phpIPAM "app code" / static token):
    This instance's API app is configured with a per-app *static token*
    (phpIPAM's app-code security mode), NOT the username/password
    user-token flow. The official API docs confirm the standard request
    carries the token verbatim in a "token" HTTP header
    (https://github.com/phpipam/phpipam/blob/master/doc/API/api_documentation.md,
    section 1.2). That means:
      * There is no login step -- no POST /api/{app_id}/user/ exchange.
      * The app code is sent verbatim in the "token" header on every
        request (phpIPAM accepts either "token" or "phpipam-token"; we
        use "token").
      * The token does not expire, so there's nothing to refresh and a
        401 is a hard failure (wrong/disabled app code, or the app's
        security mode isn't actually set to app-code) rather than a
        re-authenticate-and-retry condition.

Credentials -- two ways to supply the app token:

  1. Explicit, in code (a quick script, or an already-resolved secret):
        client = PhpIpamClient(
            base_url="https://ipam.example.com", app_id="sync",
            token="<the phpIPAM app code>",
        )

  2. From an environment variable (keeps the token out of the command
     line and the process listing):
        client = PhpIpamClient(
            base_url="...", app_id="sync",
            token_env="PHPIPAM_TOKEN",
        )

For a token held in a secret store, resolve it outside this module and
pass it in via `token`. ipamsync does exactly that: `token_command` in
`config.yml` runs any command (`ansible-vault view`, `pass`, `gpg`, a
cloud secret CLI) and uses its stdout, so no secret-store dependency
needs to live in here. See `ipamsync/config.py`.

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
    """Raised for any non-success response from the phpIPAM API, or a
    connection-level failure. Carries the HTTP status code (if any) and
    the API's own message/code fields where available, so callers can
    distinguish "not found" (404) from "validation failed" (400) from
    "server error" (500) without parsing the message string."""

    def __init__(self, message, status_code=None, api_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


class PhpIpamClient:
    """Thin wrapper around phpIPAM's REST API. One instance per
    app_id/token pair -- holds the app's static token and sends it on
    every request, so callers don't need to manage authentication state
    themselves.

    Not thread-safe in the strict sense, but the token is set once at
    construction and never mutated afterwards (unlike the old
    user-token flow, there's no mid-run refresh), so concurrent reads
    of self._token are harmless. Still single-threaded-CLI-shaped, like
    the rest of this repo's tooling.
    """

    def __init__(self, base_url, app_id, token=None, token_env=None,
                 verify_ssl=True, timeout=30):
        """
        base_url: e.g. "https://ipam.example.com" (no trailing slash,
            no /api/ suffix -- that's added automatically).
        app_id: the phpIPAM "API app" identifier (configured in phpIPAM's
            own Administration > API settings), not a phpIPAM username.
        token/token_env: the app's static token (the "app code" shown in
            phpIPAM's API app settings). Supply it directly via `token`,
            or name an environment variable that holds it via `token_env`
            (keeps the token out of the command line and the process
            listing). For a token in a secret store, resolve it outside
            this class and pass it via `token` -- see the module
            docstring.
        verify_ssl: set False only for lab/self-signed-cert instances.
            The app token is sent in a request header, so it's only as
            protected in transit as the connection itself -- keep the URL
            https:// even when disabling certificate validation, so the
            token isn't sent in clear.
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
        """Issues one API request, sending the app token in the "token"
        header. The token is a static app code (see the module
        docstring), so there's no login step and no expiry to refresh --
        a 401 is therefore a hard failure (wrong/disabled app code, or
        the app's security mode isn't actually set to app-code) and is
        surfaced as such rather than retried.

        `path` should NOT include the leading /api/{app_id}/ prefix --
        just the controller and any sub-path, e.g. "subnets/42/" or
        "addresses/".

        Returns the parsed "data" field of a successful response
        (whatever shape that field has for the given endpoint -- a
        dict, a list of dicts, or occasionally a bare value like a
        free-IP string). Raises PhpIpamError on any failure.
        """
        url = f"{self.base_url}/api/{self.app_id}/{path.lstrip('/')}"
        headers = {
            "token": self._token,
            # Sent on EVERY request, including bodyless GETs and DELETEs
            # where it describes a body that does not exist. That looks
            # redundant and is not.
            #
            # phpIPAM rejects any request content type it does not
            # recognise (api/controllers/Responses.php,
            # validate_content_type) with
            #
            #     415 Invalid Content type <value>
            #
            # Absent is accepted, and so is empty -- but anything else
            # outside {application/json, application/xml,
            # application/x-www-form-urlencoded} is refused, and that
            # includes a whitespace-only value, whose message then reads
            # as if it were truncated. Sending no header therefore leaves
            # the choice to whatever sits in front of phpIPAM: nginx,
            # php-fpm's fastcgi_param CONTENT_TYPE, a reverse proxy or a
            # WAF can each supply one. Naming ours means nothing else
            # gets to.
            #
            # Two additional notes, both verified rather than assumed:
            #
            #  * On phpIPAM <= 1.7.x the check reads `strlen(@$ct==0)` --
            #    the `==0` inside strlen(), fixed to `strlen(@$ct)==0` in
            #    1.8.1. On PHP 7 that evaluates strlen(true) -> 1, so the
            #    branch always fired and the check passed *everything*;
            #    on PHP 8 `"" == 0` is false, so it starts enforcing and
            #    an empty value is refused too. Those versions therefore
            #    break on a PHP upgrade, not a phpIPAM one.
            #
            #  * application/json is accepted by 1.5.2, 1.7.4 and 1.8.1
            #    on both PHP majors -- all six combinations checked
            #    against the real function body.
            #
            # Safe where there is no body: api/index.php only parses the
            # body after an is_blank() check, so an empty one is skipped.
            "Content-Type": "application/json",
        }

        try:
            resp = requests.request(
                method, url, headers=headers, params=params,
                json=json_body, verify=self.verify_ssl, timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise PhpIpamError(f"Connection failed: {method} {url}: {e}")

        # phpIPAM returns a JSON body even for most error cases (400,
        # 401, 404, 500) -- but fall back gracefully if a request ever
        # comes back with a non-JSON body (e.g. an upstream proxy error
        # page).
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
        """Returns the list of all sections. Safest call to smoke-test
        connectivity/auth against a real instance before trusting
        anything else here."""
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
        """Creates a new subnet. `subnet` is the network address (e.g.
        "10.0.0.0"), `mask` the prefix length (e.g. 24) -- phpIPAM
        wants these as two separate fields, not one CIDR string.
        Returns the new subnet's numeric id.

        extra_fields are passed through verbatim for any other phpIPAM
        subnet field (vlanId, vrfId, masterSubnetId, permissions, custom
        fields, etc) -- this client doesn't enumerate every possible
        field, so anything beyond the commonly-used ones above can be
        supplied directly by the caller."""
        payload = {
            "subnet": subnet,
            "mask": mask,
            "sectionId": section_id,
            **({"description": description} if description is not None else {}),
            **extra_fields,
        }
        return self._request("POST", "subnets/", json_body=payload)

    def update_subnet(self, subnet_id, **fields):
        """Partial update -- only the fields given are changed (PATCH
        semantics, matching phpIPAM's own API)."""
        return self._request("PATCH", f"subnets/{subnet_id}/", json_body=fields)

    def delete_subnet(self, subnet_id):
        return self._request("DELETE", f"subnets/{subnet_id}/")

    # -- Addresses ---------------------------------------------------------

    def get_address(self, address_id):
        return self._request("GET", f"addresses/{address_id}/")

    def search_address(self, ip):
        """Searches for an address across the whole database by IP
        (not scoped to one subnet). Returns a list -- phpIPAM's search
        endpoint can return multiple matches if the same address
        somehow exists in more than one subnet."""
        return self._request("GET", f"addresses/search/{ip}/") or []

    def create_address(self, *, subnet_id, ip=None, hostname=None,
                       description=None, **extra_fields):
        """Creates a new address. If `ip` is given, creates it at that
        specific address (subnet_id + ip both required by phpIPAM's
        addresses controller). If `ip` is omitted, uses phpIPAM's
        first_free endpoint instead to allocate the next available
        address in the subnet automatically.

        Returns the new address's numeric id (for the explicit-ip path)
        or the allocated IP string (for the first_free path) -- the two
        underlying phpIPAM endpoints have different response shapes
        (id vs the address itself), reflected here rather than papered
        over, since callers generally want different things from each
        (an id to later update/delete vs an address to actually use).

        extra_fields are passed through verbatim for any other phpIPAM
        address field (mac, owner, tag, custom fields, etc)."""
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
        """Partial update -- only the fields given are changed."""
        return self._request("PATCH", f"addresses/{address_id}/", json_body=fields)

    def delete_address(self, address_id):
        return self._request("DELETE", f"addresses/{address_id}/")

    # -- L2 domains, VLANs and VRFs ---------------------------------------
    #
    # Two naming traps live in here, both confirmed against 1.8.1 rather
    # than taken from the documentation:
    #
    #  * An L2 domain's section list is READ as `sections` and WRITTEN as
    #    `permissions`. Posting `sections` is rejected outright with
    #    `400 Invalid request key sections`. The underlying column is
    #    `vlanDomains.permissions`, and it holds a ";"-separated list of
    #    *section ids* despite the name -- see phpIPAM's own
    #    Sections::fetch_section_domains(). Domain id 1 ("default") is
    #    implicitly in every section and is not listed there.
    #  * VLANs and VRFs are read back with their primary key as `id`,
    #    not as the `vlanId`/`vrfId` the tables actually use.
    #
    # Both endpoints accept singular and plural spellings (`vlan/` and
    # `vlans/`); the plural is used here for consistency with the rest.

    def get_l2domains(self):
        return self._request("GET", "l2domains/") or []

    def create_l2domain(self, name, *, sections=None, **fields):
        """Creates an L2 domain. `sections` is an iterable of *target*
        section ids the domain should be visible in; it is sent as
        `permissions` because that is the only spelling writes accept."""
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
        """Creates a VRF. Unlike L2 domains, a VRF's section list really
        is called `sections` on both read and write -- it is a genuine
        column on the `vrf` table."""
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
