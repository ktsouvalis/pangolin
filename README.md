# pangolin-utils

A pair of TUI tools for the **Pangolin HA Cluster**:

| Script | Purpose |
|---|---|
| `monitor.py` | Real-time dashboard — polls every 20 s, one panel per service |
| `logs_viewer.py` | Log viewer — fetches warnings/errors from all nodes via SSH |

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
```

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
```

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

## Notes

- `config.yml` is git-ignored — never commit it; it contains credentials.
- Set `unicode_bullets: false` in `config.yml` if your terminal renders `●` as underscores (common in Proxmox CTs without a UTF-8 locale).
- Keepalived health is inferred from the Pangolin API: if Pangolin is reachable on a node, its VRRP priority stays at `base_priority`; otherwise `track_weight` is applied.
