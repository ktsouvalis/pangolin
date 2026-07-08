# pangolin-utils

A set of tools for the **Pangolin HA Cluster**:

| Script | Purpose |
|---|---|
| `monitor.py` | Real-time TUI dashboard — polls every 20 s, one panel per service |
| `logs_viewer.py` | TUI log viewer — fetches warnings/errors from all nodes via SSH |
| `create_private_resources.py` | CLI — batch-creates private (site) resources from a filled-in xlsx request sheet; each row becomes one resource spanning every site for HA |

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

Batch-creates Pangolin private (site) resources from a filled-in xlsx request sheet. Each row becomes **one** site resource spanning **every site in the org** (via the API's `siteIds` array), so it's reachable through any site's tunnel for HA. Not a TUI — plain CLI, prints progress to stdout.

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

Requests sheet columns: `Name | Operation System | Destination (IP or CIDR) | Alias | User Emails | Notes`

- **Operation System**: `Linux` or `Windows`. Drives a fixed TCP port policy — UDP and ICMP are always blocked, not user-configurable:
  - `Linux` → TCP `22,3389`
  - `Windows` → TCP `23579`
  - Any other/blank value fails that row locally (no API call) rather than guessing a port policy.
- **Destination**: a single IP is created as a `host` resource; a CIDR (e.g. `10.23.30.0/24`) as a `network` resource.
- **Alias**: optional FQDN (e.g. `app.internal`) to reach the resource by name instead of IP. Doesn't apply to CIDR rows; leave blank otherwise.
- **User Emails**: comma-separated university emails, resolved against the org's user list. Access is granted **only** to these users (`roleIds` is always empty — no role-based access). Unresolved emails are reported as warnings, never silently dropped.

Every run writes a `<input>_results_<timestamp>.xlsx` report next to the input file: the request contents (Name, Destination, Alias, **TCP Ports** — the resolved port string for that row's OS, not the OS itself — User Emails, Notes) plus `Sites` it spans, `Status` per site-resource (OK / FAIL / DRY-RUN), the created `niceId`, a `Timestamp` of the creation/attempt, and any unresolved emails or errors.

Filled-in request and report `.xlsx` files are git-ignored (only `*template.xlsx` is tracked) since they carry real internal IPs and emails.

> **Not yet supported:** updating an existing site resource (e.g. by its `niceId` from a previous Results sheet). Only creation is implemented today — see the `TODO` above `main()` in `create_private_resources.py`.

---

## Notes

- `config.yml` is git-ignored — never commit it; it contains credentials.
- Set `unicode_bullets: false` in `config.yml` if your terminal renders `●` as underscores (common in Proxmox CTs without a UTF-8 locale).
- Keepalived health is inferred from the Pangolin API: if Pangolin is reachable on a node, its VRRP priority stays at `base_priority`; otherwise `track_weight` is applied.
- The `nodes.newt` group may use its own SSH credentials instead of the global `ssh:` block — write it as `{ssh: {username, key_file}, hosts: [...]}` instead of a plain list. Useful when Newt hosts (e.g. remote sites) aren't reachable with the same key/user as the cluster nodes.
