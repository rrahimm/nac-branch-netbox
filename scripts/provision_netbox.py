#!/usr/bin/env python3
"""Parse a line-oriented text spec and create the corresponding region,
sites, VLANs, prefixes, and gateway IP addresses in NetBox via its REST API.

Input format (blank lines anywhere are ignored -- only these section
headers matter):

    region: <name>
    site:
    <site name>                 (one per line, repeat for each site)

    vlans for <site>
    <vlan name> : <vid>         (one per line, repeat)

    Prefixes and ip for <site>
    vlan<vid>_prefix: <cidr>
    vlan<vid>_ip: <address>

Example:

    region: nac
    site:
    unified_branch_1

    vlans for unified_branch_1
    unified_branch_1_10 : 10

    Prefixes and ip for unified_branch_1
    vlan10_prefix: 10.1.10.0/24
    vlan10_ip: 10.1.10.1

Every create is idempotent: existing region/site/tag/vlan/prefix/IP objects
(matched by slug/vid/prefix/address) are looked up first and reused rather
than duplicated, so this script is safe to re-run against the same NetBox
instance after editing the input file.

Usage:
    export NETBOX_URL=https://your-netbox-instance
    export NETBOX_TOKEN=your_api_token
    python3 scripts/provision_netbox.py path/to/spec.txt
"""
import os
import re
import sys
from pathlib import Path

import requests

GATEWAY_TAG = "gateway"


def parse_spec(text):
    region = None
    sites = []
    vlans = {}      # site -> {vid: name}
    prefixes = {}   # site -> {vid: cidr}
    gateways = {}   # site -> {vid: address}

    mode = None
    current_site = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = re.match(r"^region\s*:\s*(\S+)$", line, re.IGNORECASE)
        if m:
            region = m.group(1)
            mode = None
            continue

        if re.match(r"^site\s*:?\s*$", line, re.IGNORECASE):
            mode = "site_list"
            continue

        m = re.match(r"^vlans\s+for\s+(\S+)$", line, re.IGNORECASE)
        if m:
            current_site = m.group(1)
            vlans.setdefault(current_site, {})
            mode = "vlans"
            continue

        m = re.match(r"^prefixes\s+and\s+ip\s+for\s+(\S+)$", line, re.IGNORECASE)
        if m:
            current_site = m.group(1)
            prefixes.setdefault(current_site, {})
            gateways.setdefault(current_site, {})
            mode = "prefixes"
            continue

        if mode == "site_list":
            sites.append(line)
            continue

        if mode == "vlans":
            m = re.match(r"^(\S+)\s*:\s*(\d+)$", line)
            if not m:
                sys.exit(f"Could not parse VLAN line: {line!r}")
            name, vid = m.group(1), int(m.group(2))
            vlans[current_site][vid] = name
            continue

        if mode == "prefixes":
            m = re.match(r"^vlan(\d+)_prefix\s*:\s*(\S+)$", line, re.IGNORECASE)
            if m:
                prefixes[current_site][int(m.group(1))] = m.group(2)
                continue
            m = re.match(r"^vlan(\d+)_ip\s*:\s*(\S+)$", line, re.IGNORECASE)
            if m:
                gateways[current_site][int(m.group(1))] = m.group(2)
                continue
            sys.exit(f"Could not parse prefix/ip line: {line!r}")

        sys.exit(f"Unrecognized line outside any section: {line!r}")

    if region is None:
        sys.exit("No 'region: <name>' line found")
    if not sites:
        sys.exit("No sites found under 'site:'")

    for site in sites:
        if site not in vlans:
            sys.exit(f"No 'vlans for {site}' section found")
        if site not in prefixes:
            sys.exit(f"No 'Prefixes and ip for {site}' section found")
        for vid in vlans[site]:
            if vid not in prefixes[site]:
                sys.exit(f"{site} VLAN {vid}: missing vlan{vid}_prefix")
            if vid not in gateways[site]:
                sys.exit(f"{site} VLAN {vid}: missing vlan{vid}_ip")

    return region, sites, vlans, prefixes, gateways


def api_get(session, base_url, path, params):
    resp = session.get(f"{base_url}{path}", params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()["results"]


def api_post(session, base_url, path, payload):
    resp = session.post(f"{base_url}{path}", json=payload, timeout=15)
    if not resp.ok:
        sys.exit(f"POST {path} failed ({resp.status_code}): {resp.text}")
    return resp.json()


def get_or_create_region(session, base_url, name):
    existing = api_get(session, base_url, "/api/dcim/regions/", {"slug": name})
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/dcim/regions/", {"name": name, "slug": name})
    return obj["id"]


def get_or_create_site(session, base_url, name, region_id):
    existing = api_get(session, base_url, "/api/dcim/sites/", {"slug": name})
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/dcim/sites/", {
        "name": name, "slug": name, "region": region_id, "status": "active",
    })
    return obj["id"]


def get_or_create_tag(session, base_url, name):
    existing = api_get(session, base_url, "/api/extras/tags/", {"name": name})
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/extras/tags/", {
        "name": name, "slug": name,
        "description": "Marks the gateway IP address for a prefix",
    })
    return obj["id"]


def get_or_create_vlan(session, base_url, site_id, vid, name):
    existing = api_get(
        session, base_url, "/api/ipam/vlans/", {"site_id": site_id, "vid": vid}
    )
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/ipam/vlans/", {
        "site": site_id, "vid": vid, "name": name,
    })
    return obj["id"]


def get_or_create_prefix(session, base_url, cidr, site_id, vlan_id, description):
    existing = api_get(
        session, base_url, "/api/ipam/prefixes/", {"prefix": cidr, "site_id": site_id}
    )
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/ipam/prefixes/", {
        "prefix": cidr, "scope_type": "dcim.site", "scope_id": site_id,
        "vlan": vlan_id, "description": description,
    })
    return obj["id"]


def get_or_create_ip(session, base_url, address, tag_id, description):
    existing = api_get(session, base_url, "/api/ipam/ip-addresses/", {"address": address})
    if existing:
        return existing[0]["id"]
    obj = api_post(session, base_url, "/api/ipam/ip-addresses/", {
        "address": address, "tags": [tag_id], "description": description,
    })
    return obj["id"]


def main():
    if len(sys.argv) != 2:
        sys.exit(f"Usage: {sys.argv[0]} <spec.txt>")

    spec_path = Path(sys.argv[1])
    region_name, sites, vlans, prefixes, gateways = parse_spec(spec_path.read_text())

    base_url = os.environ.get("NETBOX_URL")
    token = os.environ.get("NETBOX_TOKEN")
    if not base_url or not token:
        sys.exit("NETBOX_URL and NETBOX_TOKEN must be set in the environment")
    base_url = base_url.rstrip("/")

    session = requests.Session()
    session.headers["Authorization"] = f"Token {token}"

    region_id = get_or_create_region(session, base_url, region_name)
    print(f"region {region_name} -> id {region_id}")

    tag_id = get_or_create_tag(session, base_url, GATEWAY_TAG)
    print(f"tag {GATEWAY_TAG} -> id {tag_id}")

    for site in sites:
        site_id = get_or_create_site(session, base_url, site, region_id)
        print(f"site {site} -> id {site_id}")

        for vid, vlan_name in sorted(vlans[site].items()):
            vlan_id = get_or_create_vlan(session, base_url, site_id, vid, vlan_name)
            print(f"  vlan {vlan_name} (vid {vid}) -> id {vlan_id}")

            cidr = prefixes[site][vid]
            prefix_id = get_or_create_prefix(
                session, base_url, cidr, site_id, vlan_id,
                f"{site} VLAN {vid} subnet",
            )
            print(f"    prefix {cidr} -> id {prefix_id}")

            gw = gateways[site][vid]
            gw_address = f"{gw}/{cidr.split('/')[1]}"
            ip_id = get_or_create_ip(
                session, base_url, gw_address, tag_id,
                f"Gateway for {site} VLAN {vid}",
            )
            print(f"    gateway ip {gw_address} -> id {ip_id}")

    print("Done.")


if __name__ == "__main__":
    main()
