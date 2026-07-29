# pangolin-utils

A set of tools for the **Pangolin HA Cluster**:

| Script | Purpose |
|---|---|
| `monitor.py` | Real-time TUI dashboard — polls every 20 s, one panel per service |
| `logs_viewer.py` | TUI log viewer — fetches warnings/errors from all nodes via SSH |
| `create_private_resources.py` | CLI — batch-creates private (site) resources from a filled-in xlsx request sheet; each User Emails entry becomes one resource spanning every site for HA |
| `normalize_private_resources.py` | CLI — audits existing private resources against that naming convention and fixes drift; backfills access for resources created without a matching user |

Built with [Textual](https://textual.textualize.io/). No agents, no daemons — runs from any workstation that can reach the cluster network.

---

## What it monitors

| Panel | How |
|---|---|
| **VIP / Keepalived** | HTTP to Pangolin API on VIP — confirms VIP is reachable; VRRP priority calculated per node |
| **Pangolin / Gerbil backends** | `GET http://<node>:3001/api/v1/` — API health; Gerbil inferred from same endpoint (shared compose stack) |
| **HAProxy backends** | Parses `/stats;csv` — shows per-backend UP/DOWN count, request rate, 5xx errors per node |
| **PostgreSQL / Patroni** | `GET http://<node>:8008/` — role (LEADER/REPLICA), state, timeline, replication lag, last failover |
| **etcd** | `GET http://<node>:2379/health` + `/v3/maintenance/status` — health, leader, raft term, DB size |
| **Newt agents** | SSH connect attempt only (no container inspection) — reachable/unreachable per host. Panel only appears if `nodes.newt` is configured |

---

## Color coding

| Indicator | Meaning |
|---|---|
| ${\color{green}●}$ Green | Service is up and in primary/active/leader role |
| ${\color{gray}●}$ Grey | Service is up but in backup/replica/follower role (healthy, non-primary) |
| ${\color{yellow}●}$ Yellow | Degraded — partial backends UP or 2 nodes failing |
| ${\color{red}●}$ Red | Service is down or unreachable |
| ${\color{green}●}$ Top banner green | All services across all nodes are healthy |
| ${\color{red}●}$ Top banner red | One or more services are down |

---

## Requirements

- Python 3.10+
- Network access to all cluster node IPs
- Pangolin API accessible (port 3001)
- Patroni REST API accessible (port 8008)
- etcd HTTP API accessible (port 2379)
- HAProxy stats endpoint enabled (port 9000)
- SSH key access to all nodes (for `logs_viewer.py`)
- Pangolin **Integration API** enabled + an org API key (for `create_private_resources.py` — see below, this is a separate opt-in feature from the dashboard)

---

## Installation

1. Clone the repo and set up a Python environment:
```bash
git clone <repo> pangolin-utils
cd pangolin-utils
```

2. Create and activate a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
```
or
```bash
conda create -n pangolin-utils python=3.11
conda activate pangolin-utils
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

---

## Configuration

All settings are driven by a single YAML config file.

```bash
cp config.yml.example config.yml
nano config.yml     # fill in your IPs, node names, credentials
```

Run with the default config file:
```bash
python3 monitor.py
```

Or specify a custom config file:
```bash
python3 monitor.py /path/to/config.yml
```

`create_private_resources.py` additionally needs a `pangolin:` block in `config.yml` (API access, not cluster monitoring). `base_url` must point at Pangolin's **Integration API**, not the dashboard — this is a separate opt-in service that must be enabled server-side first (see "Private resource creation" below):
```yaml
pangolin:
  base_url: "https://pangolin.uop.gr/int-api"
  org_slug: "your-org-slug"
  api_key: "..."
```

---

## Key bindings (monitor.py)

| Key | Action |
|---|---|
| `R` | Force immediate refresh |
| `Q` | Quit |

---

## Log viewer (logs_viewer.py)

Collects warnings and errors from the last 24 hours across every service and node via SSH. Systemd services are read from `journald`; containerised services are read from `docker logs`.

### TUI mode

Node tabs across the top; service sub-tabs within each node. Results stream in per service as SSH calls complete.

```bash
python3 logs_viewer.py
python3 logs_viewer.py --config /path/to/config.yml
python3 logs_viewer.py --last 6      # override lookback window (hours) for both WARN/ERROR and Newt access logs
```

If any node in the `newt` group is configured, an extra **Access Logs** sub-tab appears per node showing parsed `ACCESS START`/`END` session pairs (last 7 days by default), resolved against the Pangolin database into user/client/resource names.

Key bindings:

| Key | Action |
|---|---|
| `R` | Re-fetch all logs |
| `Q` | Quit |

### Save mode

Fetches all logs and writes a structured plain-text `.log` file — no TUI is shown. Progress is printed to stdout as each result arrives. The `.log` extension is appended automatically if omitted.

```bash
python3 logs_viewer.py --save cluster_logs
# writes: cluster_logs.log
# if a "newt" node group is configured, also writes: cluster_logs_newt.csv
```

The `_newt.csv` file contains resolved Newt ACCESS sessions (who connected, to which resource, proto, duration) for the last 7 days — pulled from Newt's Docker logs and cross-referenced against the Pangolin database (`sites`, `clients`, `siteResources` tables).

Output format:

```
Pangolin HA Cluster — Log Report
Fetched:  2026-05-26 15:30:00
Scope:    last 24h, warnings and errors only
================================================================================

NODE: pangolin-node-1  (10.99.97.51)
================================================================================

  SERVICE: Pangolin  [docker: pangolin]
  ────────────────────────────────────────────────────────────
  2026-05-26 14:01:33 WARNING  …
  2026-05-26 14:22:11 ERROR    …

  SERVICE: Patroni  [systemd: patroni]
  ────────────────────────────────────────────────────────────
  (no warnings or errors in the last 24h)
…
```

### Configuring services

The list of services to poll is defined in `config.yml` under the `services:` key. Each entry specifies a display label, which node group it runs on, whether it is a Docker container or a systemd unit, and the container/unit name.

```yaml
services:
  - label: "Pangolin"
    nodes: pangolin        # key from the nodes: or keepalived: sections
    type: docker
    container: "pangolin"

  - label: "Patroni"
    nodes: patroni
    type: systemd
    unit: "patroni"
```

`nodes` must match one of the keys already present in the `nodes:` map (or `keepalived`). Services can be added, removed, or renamed here without touching the code.

### Additional requirements for logs_viewer.py

- SSH access to all cluster nodes (username + key configured under `ssh:` in `config.yml`)
- Docker CLI available on each node (`docker logs`)
- `systemd` / `journalctl` available on nodes running bare-metal services

---

## Private resource creation (create_private_resources.py)

Batch-creates Pangolin private (site) resources from a filled-in xlsx request sheet. Each **User Emails** entry on a row becomes its **own** site resource (a row with 3 emails creates 3 resources), each spanning **every site in the org** (via the API's `siteIds` array), so it's reachable through any site's tunnel for HA. Not a TUI — plain CLI, prints progress to stdout.

### One-time server-side setup: enabling the Integration API

This script talks to Pangolin's **Integration API**, a separate opt-in service — not the dashboard's own internal API. It must be enabled per-node before this script (or any Bearer-token API access) will work at all:

1. In each Pangolin node's own `config.yml` (`/opt/pangolin/config/config.yml` inside the `pangolin` container — **not** this repo's `config.yml`), add:
   ```yaml
   flags:
       enable_integration_api: true
   server:
       integration_port: 3003   # default
   ```
2. In each node's Traefik `dynamic_config.yml`, add a router exposing that port. This cluster reuses the existing `pangolin.uop.gr` domain/cert via a `/int-api` path prefix (with a `stripPrefix` middleware) rather than provisioning a new subdomain:
   ```yaml
   middlewares:
     int-api-stripprefix:
       stripPrefix:
         prefixes: ["/int-api"]
   routers:
     int-api-router:
       rule: "Host(`pangolin.uop.gr`) && PathPrefix(`/int-api`)"
       service: int-api-service
       entryPoints: [websecure]
       middlewares: [int-api-stripprefix, badger]
       tls:
         certResolver: letsencrypt
   services:
     int-api-service:
       loadBalancer:
         servers:
           - url: "http://127.0.0.1:3003"
   ```
3. Restart the `pangolin` and `traefik` containers on that node.

Each node's config is independent (no shared filesystem) — repeat on **all** nodes, one at a time (the keepalived VIP fails over to the others during each restart).

> **Status (2026-07-08): applied on nodes 2 and 3 only.** Node 1 (`10.99.97.51`) still needs this same change — it was unreachable during an ongoing genset maintenance window. Until node 1 is updated, a VIP failover to node 1 will make this script fail even though it works from nodes 2/3. Confirm which node currently holds the VIP (`monitor.py`, or `ip a` / keepalived logs on a node) before assuming the API is available.

### Usage

Start from the template, fill in the `Requests` tab (see its `Instructions` tab for column meanings), then run:

```bash
python3 create_private_resources.py pangolin_private_resources_template.xlsx --dry-run
python3 create_private_resources.py my_requests.xlsx
python3 create_private_resources.py my_requests.xlsx --config /path/to/config.yml
```

Requests sheet columns: `Name | Operation System | Destination (IP or CIDR) | Ports | Alias | User Emails | Notes`

- **Name**: the city, filled in by the Digital Governance Unit (not the requester) — it's the first segment of the computed resource name (see below), not a free-form label.
- **Operation System**: `Linux` or `Windows`. Informational only — reported alongside the resource but no longer drives port selection.
- **Destination**: a single IP is created as a `host` resource; a CIDR (e.g. `10.23.30.0/24`) as a `network` resource.
- **Ports**: comma-separated TCP port numbers (e.g. `22,3389`), user-entered per row. UDP and ICMP are always blocked, not user-configurable. A blank value or any non-numeric token fails that row locally (no API call) rather than guessing a port policy.
- **Alias**: optional FQDN (e.g. `app.internal`) to reach the resource by name instead of IP. Doesn't apply to CIDR rows; leave blank otherwise.
- **User Emails**: comma-separated university emails. **Each email becomes its own site resource** — a row with 3 emails creates 3 resources, each granting access to only that one resolved user (`roleIds` is always empty — no role-based access). An email that doesn't match any current org user still gets its resource created (the name never depended on the lookup succeeding) but with no user attached — reported as `OK_NO_USER`/`DRY-RUN_NO_USER` rather than skipped. Run `normalize_private_resources.py` later, once the account exists, to backfill access. A row is only skipped locally (no API call, reported `FAIL`) for invalid `Ports` or an unparseable `Destination`, or if it has no emails at all.

Resource names are computed, not typed in: `<city>-<username>-<vlan>[-<z>]`, where `city` is the (lowercased) `Name` column, `username` is the part of the email before `@` with any `.` stripped (e.g. `m.katsis` → `mkatsis`), and `vlan`/`z` come from the Destination's `10.x.y.z` octets — `y` zero-padded to 2 digits, `z` left unpadded, and `z` dropped entirely for CIDR destinations. Example: `ktsouvalis@uop.gr` at `10.23.2.50`, city "Patra" → `patra-ktsouvalis-2302-50`.

Every run writes a `<input>_results_<timestamp>.xlsx` report next to the input file, one row per attempted resource (or per skipped email): `City`, computed `Name`, `Destination`, `Alias`, **TCP Ports** — the row's `Ports` value as parsed — `Email`, `Notes`, `Sites` it spans, `Status` (`OK` / `OK_NO_USER` / `FAIL` / `DRY-RUN` / `DRY-RUN_NO_USER`), the created `niceId`, a `Timestamp` of the attempt, and any `Error` (including the no-user-match note or a row with no emails at all).

Filled-in request and report `.xlsx` files are git-ignored (only `*template.xlsx` is tracked) since they carry real internal IPs and emails.

> **Not yet supported:** updating an existing site resource (e.g. by its `niceId` from a previous Results sheet). Only creation is implemented today — see the `TODO` above `main()` in `create_private_resources.py`.

---

## Private resource normalization (normalize_private_resources.py)

Audits *existing* Pangolin private resources — fetched live via the Integration API, not from a spreadsheet — against the naming convention above, and fixes the ones that drift from it. Unlike `create_private_resources.py`, it covers both `host` and `cidr` resources:

- **host**: `<city>-<username>-<vlan>-<z>`, same as resource creation.
- **cidr**: `<city>-<username>-<vlan>-<tail>`, where `vlan` is built from however many octets the subnet mask actually fixes (dropped below `/16`), and `tail` is `all` for the common `/24` case or the literal prefix length otherwise (e.g. `/16` → `16`), so an unusual mask is never mistaken for a `/24`.

`city` is taken from the resource's *existing* name (its first `-`-separated segment) since the API has no separate city field. The target user (whose sanitized email local part becomes the username segment, and who ends up as the resource's *only* grantee) is resolved from who currently has access:

- exactly 1 user assigned → that user, unconditionally.
- 2+ users assigned → resolved only if exactly one of them has a sanitized email matching a segment of the existing name (i.e. the name already seems to identify a primary owner among several grantees); otherwise skipped as ambiguous.
- 0 users assigned → this is the expected state for a resource `create_private_resources.py` created for an email that didn't match any org user yet (its `OK_NO_USER` status). Auto-resolved and granted (`action=grant_access`, no confirmation needed) **only** when the name is an *exact* reconstruction of the naming convention: peeling the known city and known vlan/tail (from the destination) off the name leaves exactly one org user's sanitized local part — the literal reverse of `create_private_resources.py`'s own naming formula, not a guess. Anything looser (name isn't an exact match, or the leftover matches zero/multiple org users) is skipped instead, with a best-effort suggested email for a human to confirm and grant by hand.

It never grants access on a loose guess — only the exact-reconstruction case above is auto-applied; a genuinely ambiguous or non-conforming resource is always reported and left alone.

```bash
python3 normalize_private_resources.py                                    # dry run, full report
python3 normalize_private_resources.py --apply                            # apply for real (confirms first)
python3 normalize_private_resources.py --apply --resource-id 130 --yes    # apply to one resource, no prompt
```

Defaults to a dry run — no changes are made unless `--apply` is passed, and a full unscoped `--apply` asks for interactive confirmation first (skip it with `--yes`, or scope to specific resources with one or more `--resource-id`). Every run writes a `private_resources_normalize_<timestamp>.xlsx` report (git-ignored, same as the request/report files above) listing every resource considered, what would change (or did), and why anything was skipped.

Roles and clients granted on a resource are read and reported but never modified — every resource in this org, including ones this tooling created, carries the org's baseline `Admin` role and no clients regardless of what's requested, which looks like a server-side default rather than per-resource state either script manages. A resource with anything else attached is flagged as an anomaly in the report instead of being silently changed.

---

## Notes

- `config.yml` is git-ignored — never commit it; it contains credentials.
- Set `unicode_bullets: false` in `config.yml` if your terminal renders `●` as underscores (common in Proxmox CTs without a UTF-8 locale).
- Keepalived health is inferred from the Pangolin API: if Pangolin is reachable on a node, its VRRP priority stays at `base_priority`; otherwise `track_weight` is applied.
- The `nodes.newt` group may use its own SSH credentials instead of the global `ssh:` block — write it as `{ssh: {username, key_file}, hosts: [...]}` instead of a plain list. Useful when Newt hosts (e.g. remote sites) aren't reachable with the same key/user as the cluster nodes.
