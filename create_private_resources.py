#!/usr/bin/env python3
"""
Create Pangolin private (site) resources from a filled-in xlsx request sheet.
Each User Emails entry on a row becomes its OWN site resource (so a row with 3
emails creates 3 resources), each spanning every site in the org (via the
API's siteIds array) for HA. Writes a results report alongside the input file.

Config (config.yml, key-value, nested under 'pangolin'):
    pangolin:
      base_url: "https://pangolin.uop.gr"
      org_slug: "university-of-the-peloponnese"
      api_key: "..."

Requests sheet columns (see pangolin_private_resources_template.xlsx):
    Name | Operation System | Destination (IP or CIDR) | Ports | Alias | User Emails | Notes

Name: the city (filled in by the Digital Governance Unit, not the requester),
      used as the first segment of the computed resource name.

Alias: optional FQDN (e.g. "app.internal") to reach the resource by name instead
       of IP. Not applicable when Destination is a CIDR range; left blank most
       of the time.

Ports: comma-separated TCP port numbers (e.g. "22,3389"), user-entered.
       UDP and ICMP are always blocked, not user-configurable. Missing/blank
       or any non-numeric token fails that row locally (no API call) rather
       than guessing a port policy. Operation System is informational only
       (no longer used to derive ports).

User Emails: comma-separated. Each email becomes a separate site resource,
             named "<city>-<username>-<vlan>[-<z>]" where username is the part
             of the email before "@" with any "." removed (e.g. m.katsis ->
             mkatsis), and vlan/z come from Destination's 10.x.y.z octets
             (y zero-padded to 2 digits, z not padded; z is dropped for CIDR
             destinations). E.g. ktsouvalis@uop.gr at 10.23.2.50 with city
             "patra" -> patra-ktsouvalis-2302-50.
             Unresolved emails are skipped (no resource created) and reported
             as a FAIL row, never silently dropped from the report.

Usage:
    python3 create_private_resources.py requests.xlsx --dry-run
    python3 create_private_resources.py requests.xlsx
    python3 create_private_resources.py requests.xlsx --config /path/to/config.yml
"""

import argparse
import datetime
import sys

import requests
import yaml
from openpyxl import load_workbook
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter

session = requests.Session()


def load_config(path):
    with open(path, "r") as f:
        cfg = yaml.safe_load(f)
    pg = cfg.get("pangolin", {})
    for key in ("base_url", "org_slug", "api_key"):
        if not pg.get(key):
            sys.exit(f"ERROR: missing 'pangolin.{key}' in {path}")
    pg["base_url"] = pg["base_url"].rstrip("/")
    return pg


def api_headers(cfg):
    return {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}


def verify_org(cfg):
    resp = session.get(f"{cfg['base_url']}/v1/org/{cfg['org_slug']}", headers=api_headers(cfg))
    resp.raise_for_status()
    org = resp.json()["data"]
    print(f"Org OK: {org.get('name', cfg['org_slug'])} (orgId={cfg['org_slug']})")
    return org


def get_all_pages(cfg, path, data_key, page_size=1000):
    items, page = [], 1
    while True:
        resp = session.get(f"{cfg['base_url']}/v1{path}", headers=api_headers(cfg),
                            params={"pageSize": page_size, "page": page})
        resp.raise_for_status()
        body = resp.json()
        page_items = body["data"][data_key]
        items.extend(page_items)
        total = body["data"].get("pagination", {}).get("total", len(items))
        page += 1
        if len(items) >= total or not page_items:
            break
    return items


def get_sites(cfg):
    sites = get_all_pages(cfg, f"/org/{cfg['org_slug']}/sites", "sites")
    if not sites:
        sys.exit("ERROR: no sites found in this org.")
    return sites


def build_user_index(cfg):
    users = get_all_pages(cfg, f"/org/{cfg['org_slug']}/users", "users")
    index = {}
    for u in users:
        email = (u.get("email") or u.get("user", {}).get("email") or "").lower()
        uid = u.get("id") or u.get("user", {}).get("id")
        if email and uid:
            index[email] = uid
    return index


def parse_tcp_ports(raw_value):
    """Parse the "Ports" column into a normalized comma-separated TCP port
    string, or None if missing/invalid. UDP and ICMP are always blocked, so
    they're not user-configurable inputs (see CLAUDE.md)."""
    raw = str(raw_value).strip() if raw_value else ""
    tokens = [p.strip() for p in raw.split(",") if p.strip()]
    if not tokens or not all(p.isdigit() and 1 <= int(p) <= 65535 for p in tokens):
        return None
    return ",".join(tokens)


def compute_vlan_z(destination, mode):
    """Derive the vlan/z name segments from a 10.x.y.z destination.

    vlan = x concatenated with y zero-padded to 2 digits (e.g. 10.23.2.50 ->
    "2302"); z is the 4th octet, not padded, and omitted entirely for CIDR
    destinations (no single host octet to point at).
    """
    ip_part = destination.split("/", 1)[0].strip()
    parts = ip_part.split(".")
    if len(parts) != 4 or not all(p.isdigit() for p in parts):
        return None, None
    x, y, z = parts[1], parts[2], parts[3]
    vlan = f"{int(x)}{int(y):02d}"
    z_val = None if mode == "cidr" else str(int(z))
    return vlan, z_val


def parse_row(row_num, row, user_index):
    """Return (resolved_reqs, fail_results) for a row.

    Each User Emails entry becomes its own resource request; anything that
    can't produce one (missing emails, bad Ports, unparseable destination,
    unresolved email) becomes a FAIL result instead, without calling the API.
    """
    city, os_value, destination, ports_value, alias, emails, notes = row
    if not destination:
        return [], []

    destination = str(destination).strip()
    mode = "cidr" if "/" in destination else "host"
    alias = str(alias).strip() if alias else None
    os_display = str(os_value).strip() if os_value else ""
    ports_display = str(ports_value).strip() if ports_value else ""
    tcp_ports = parse_tcp_ports(ports_value)
    city = str(city).strip() if city else ""
    notes_val = str(notes).strip() if notes else None
    vlan, z_val = compute_vlan_z(destination, mode)

    raw_emails = [e.strip() for e in str(emails or "").split(",") if e.strip()]

    def make_fail(email, error):
        return {
            "row_num": row_num, "city": city, "name": None, "destination": destination,
            "alias": alias, "tcp_ports": tcp_ports, "email": email, "notes": notes_val,
            "sites": None, "status": "FAIL", "nice_id": None,
            "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "error": error,
        }

    if not raw_emails:
        return [], [make_fail(None, "no User Emails provided")]

    resolved_reqs, fail_results = [], []
    for raw_email in raw_emails:
        email = raw_email.lower()
        if tcp_ports is None:
            fail_results.append(make_fail(raw_email,
                f"invalid/missing Ports {ports_display!r} "
                f"(expected comma-separated TCP port numbers)"))
            continue
        if vlan is None:
            fail_results.append(make_fail(raw_email,
                f"destination {destination!r} is not a valid IPv4 host/CIDR"))
            continue
        if email not in user_index:
            fail_results.append(make_fail(raw_email, f"unresolved email: {raw_email}"))
            continue

        username = email.split("@", 1)[0].replace(".", "")
        name_parts = [city.lower(), username, vlan] + ([z_val] if z_val is not None else [])
        resource_name = "-".join(p for p in name_parts if p)

        req = {
            "_row_num": row_num,
            "_city": city,
            "_notes": notes_val,
            "_email": raw_email,
            "name": resource_name,
            "mode": mode,
            "destination": destination,
            "os": os_display,
            "tcpPortRangeString": tcp_ports,
            "udpPortRangeString": "",
            "disableIcmp": True,
            "roleIds": [],
            "clientIds": [],
            "userIds": [user_index[email]],
        }
        if alias:
            req["alias"] = alias
        resolved_reqs.append(req)

    return resolved_reqs, fail_results


def create_site_resource(cfg, sites, req, dry_run):
    payload = {k: v for k, v in req.items() if not k.startswith("_") and k != "os"}
    payload["siteIds"] = [s["siteId"] for s in sites]
    site_names = ", ".join(s["name"] for s in sites)

    result = {
        "row_num": req["_row_num"],
        "city": req["_city"],
        "name": req["name"],
        "destination": req["destination"],
        "alias": req.get("alias"),
        "tcp_ports": req["tcpPortRangeString"] or None,
        "email": req["_email"],
        "notes": req["_notes"],
        "sites": site_names,
        "status": None,
        "nice_id": None,
        "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "error": None,
    }

    if dry_run:
        printable = {k: v for k, v in payload.items() if k not in ("roleIds", "clientIds")}
        print(f"  [DRY-RUN] {printable}")
        result["status"] = "DRY-RUN"
        return result

    resp = session.put(f"{cfg['base_url']}/v1/org/{cfg['org_slug']}/site-resource",
                        headers=api_headers(cfg), json=payload)

    if resp.status_code >= 400:
        print(f"  [FAIL] {resp.status_code}: {resp.text}")
        result["status"] = "FAIL"
        result["error"] = f"{resp.status_code}: {resp.text[:500]}"
        return result

    body = resp.json()["data"]
    result["status"] = "OK"
    result["nice_id"] = body["niceId"]
    print(f"  [OK] siteResourceId={body['siteResourceId']} niceId={body['niceId']} "
          f"sites=[{site_names}]")
    return result


def write_report(input_path, results):
    wb = load_workbook(input_path)
    if "Results" in wb.sheetnames:
        del wb["Results"]
    ws = wb.create_sheet("Results")

    headers = ["Row", "City", "Name", "Destination", "Alias", "TCP Ports", "Email", "Notes", "Sites",
               "Status", "Nice ID", "Timestamp", "Error"]
    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(name="Arial", size=11, bold=True, color="FFFFFF")

    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center")

    status_colors = {
        "OK": "C6EFCE",
        "FAIL": "FFC7CE",
        "DRY-RUN": "FFEB9C",
    }

    for r, res in enumerate(results, start=2):
        values = [
            res["row_num"], res["city"], res["name"], res["destination"], res["alias"],
            res["tcp_ports"], res["email"], res["notes"], res["sites"],
            res["status"], res["nice_id"], res["timestamp"], res["error"],
        ]
        for c, v in enumerate(values, start=1):
            ws.cell(row=r, column=c, value=v)
        fill_color = status_colors.get(res["status"])
        if fill_color:
            ws.cell(row=r, column=10).fill = PatternFill("solid", fgColor=fill_color)

    widths = [6, 14, 24, 22, 22, 14, 26, 30, 30, 10, 26, 18, 40]
    for c, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(c)].width = w

    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = input_path.rsplit(".", 1)[0] + f"_results_{ts}.xlsx"
    wb.save(out_path)
    return out_path


# TODO: add an --update mode to modify an existing site resource identified by
# its niceId from the Results sheet (resolve niceId -> siteResourceId via
# GET /org/{orgId}/site-resources, then POST /site-resource/{siteResourceId}).
# Only creation is supported today.
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("xlsx_path", help="path to the filled-in requests xlsx")
    parser.add_argument("--sheet", default="Requests")
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    cfg = load_config(args.config)
    verify_org(cfg)

    sites = get_sites(cfg)
    print(f"Found {len(sites)} site(s):")
    for s in sites:
        print(f"  - {s['name']} (siteId={s['siteId']}, online={s.get('online')})")

    user_index = build_user_index(cfg)

    wb = load_workbook(args.xlsx_path, data_only=True)
    ws = wb[args.sheet]
    raw_rows = list(ws.iter_rows(min_row=2, max_col=7, values_only=True))

    requests_parsed, upfront_fails = [], []
    for i, row in enumerate(raw_rows, start=2):
        resolved, fails = parse_row(i, row, user_index)
        requests_parsed.extend(resolved)
        upfront_fails.extend(fails)

    print(f"\nParsed {len(raw_rows)} row(s) from '{args.xlsx_path}': "
          f"{len(requests_parsed)} resource(s) to create "
          f"(one per resolved User Emails entry), {len(upfront_fails)} skipped\n")

    all_results = []
    for req in requests_parsed:
        print(f"- row {req['_row_num']}: {req['name']} ({req['mode']}: {req['destination']}) "
              f"email={req['_email']} "
              f"alias={req.get('alias') or '-'} "
              f"os={req['os'] or '-'} "
              f"tcp={req['tcpPortRangeString'] or 'blocked'} "
              f"udp=blocked icmp=blocked")
        all_results.append(create_site_resource(cfg, sites, req, args.dry_run))

    for fail in upfront_fails:
        print(f"  [FAIL] row {fail['row_num']}: {fail['error']}")
    all_results.extend(upfront_fails)
    all_results.sort(key=lambda r: r["row_num"])

    out_path = write_report(args.xlsx_path, all_results)
    print(f"\nReport written to: {out_path}")

    fails = [r for r in all_results if r["status"] == "FAIL"]
    if fails:
        print(f"WARNING: {len(fails)} row(s)/email(s) failed or were skipped — see report.")


if __name__ == "__main__":
    main()