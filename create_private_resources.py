#!/usr/bin/env python3
"""
Create Pangolin private (site) resources from a filled-in xlsx request sheet.
Each row becomes a single site resource spanning every site in the org (via
the API's siteIds array), so it's reachable through any site's tunnel for HA.
Writes a results report alongside the input file.

Config (config.yml, key-value, nested under 'pangolin'):
    pangolin:
      base_url: "https://pangolin.uop.gr"
      org_slug: "university-of-the-peloponnese"
      api_key: "..."

Requests sheet columns (see pangolin_private_resources_template.xlsx):
    Name | Destination (IP or CIDR) | Alias | TCP Ports | UDP Ports | ICMP | User Emails | Notes

Alias: optional FQDN (e.g. "app.internal") to reach the resource by name instead
       of IP. Not applicable when Destination is a CIDR range; left blank most
       of the time.

TCP/UDP Ports values:
    "all"      -> "*"   (every port)
    "blocked"  -> ""    (protocol fully blocked)
    anything else used as-is (e.g. "80,443" or "8000-9000,443")

ICMP values: "Enabled" / "Disabled"
User Emails: comma-separated; each resolved to a user ID (unresolved emails are
             reported as warnings and never silently dropped from the report).

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


def port_string(value):
    v = (value or "").strip().lower()
    if v == "all":
        return "*"
    if v in ("blocked", ""):
        return ""
    return value.strip()


def parse_row(row_num, row, user_index):
    name, destination, alias, tcp, udp, icmp, emails, notes = row
    if not destination:
        return None

    destination = str(destination).strip()
    mode = "cidr" if "/" in destination else "host"
    alias = str(alias).strip() if alias else None

    resolved_users, unresolved = [], []
    for raw_email in str(emails or "").split(","):
        email = raw_email.strip().lower()
        if not email:
            continue
        if email in user_index:
            resolved_users.append(user_index[email])
        else:
            unresolved.append(email)

    req = {
        "_row_num": row_num,
        "name": (str(name).strip() if name else destination),
        "mode": mode,
        "destination": destination,
        "tcpPortRangeString": port_string(tcp),
        "udpPortRangeString": port_string(udp),
        "disableIcmp": str(icmp or "").strip().lower() == "disabled",
        "roleIds": [],
        "clientIds": [],
        "userIds": resolved_users,
        "_unresolved_emails": unresolved,
        "_notes": notes,
    }
    if alias:
        req["alias"] = alias
    return req


def create_site_resource(cfg, sites, req, dry_run):
    payload = {k: v for k, v in req.items() if not k.startswith("_")}
    payload["siteIds"] = [s["siteId"] for s in sites]
    site_names = ", ".join(s["name"] for s in sites)

    result = {
        "row_num": req["_row_num"],
        "name": req["name"],
        "destination": req["destination"],
        "alias": req.get("alias"),
        "sites": site_names,
        "status": None,
        "site_resource_id": None,
        "nice_id": None,
        "http_status": None,
        "error": None,
        "unresolved_emails": ", ".join(req["_unresolved_emails"]) or None,
    }

    if dry_run:
        print(f"  [DRY-RUN] {payload}")
        result["status"] = "DRY-RUN"
        return result

    resp = session.put(f"{cfg['base_url']}/v1/org/{cfg['org_slug']}/site-resource",
                        headers=api_headers(cfg), json=payload)
    result["http_status"] = resp.status_code

    if resp.status_code >= 400:
        print(f"  [FAIL] {resp.status_code}: {resp.text}")
        result["status"] = "FAIL"
        result["error"] = resp.text[:500]
        return result

    body = resp.json()["data"]
    result["status"] = "OK"
    result["site_resource_id"] = body["siteResourceId"]
    result["nice_id"] = body["niceId"]
    print(f"  [OK] siteResourceId={body['siteResourceId']} niceId={body['niceId']} "
          f"sites=[{site_names}]")
    return result


def write_report(input_path, results):
    wb = load_workbook(input_path)
    if "Results" in wb.sheetnames:
        del wb["Results"]
    ws = wb.create_sheet("Results")

    headers = ["Row", "Name", "Destination", "Alias", "Sites", "Status",
               "Site Resource ID", "Nice ID", "HTTP Status", "Unresolved Emails", "Error"]
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
            res["row_num"], res["name"], res["destination"], res["alias"], res["sites"],
            res["status"], res["site_resource_id"], res["nice_id"], res["http_status"],
            res["unresolved_emails"], res["error"],
        ]
        for c, v in enumerate(values, start=1):
            ws.cell(row=r, column=c, value=v)
        fill_color = status_colors.get(res["status"])
        if fill_color:
            ws.cell(row=r, column=6).fill = PatternFill("solid", fgColor=fill_color)

    widths = [6, 22, 22, 22, 30, 10, 16, 26, 12, 30, 40]
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
    raw_rows = list(ws.iter_rows(min_row=2, max_col=8, values_only=True))

    requests_parsed = []
    for i, row in enumerate(raw_rows, start=2):
        parsed = parse_row(i, row, user_index)
        if parsed:
            requests_parsed.append(parsed)

    print(f"\nParsed {len(requests_parsed)} request row(s) from '{args.xlsx_path}'")
    print(f"Creating {len(requests_parsed)} site-resource(s), each spanning all "
          f"{len(sites)} site(s)\n")

    all_results = []
    for req in requests_parsed:
        print(f"- row {req['_row_num']}: {req['name']} ({req['mode']}: {req['destination']}) "
              f"alias={req.get('alias') or '-'} "
              f"tcp={req['tcpPortRangeString'] or 'blocked'} "
              f"udp={req['udpPortRangeString'] or 'blocked'} "
              f"icmp={'disabled' if req['disableIcmp'] else 'enabled'}")
        if req["_unresolved_emails"]:
            print(f"  [WARN] unresolved email(s): {req['_unresolved_emails']}")
        all_results.append(create_site_resource(cfg, sites, req, args.dry_run))

    out_path = write_report(args.xlsx_path, all_results)
    print(f"\nReport written to: {out_path}")

    fails = [r for r in all_results if r["status"] == "FAIL"]
    warns = [r for r in all_results if r["unresolved_emails"]]
    if fails:
        print(f"WARNING: {len(fails)} creation(s) failed — see report.")
    if warns:
        print(f"WARNING: {len(warns)} row(s) had unresolved email(s) — see report.")


if __name__ == "__main__":
    main()