# CLAUDE.md

Guidance for working in this repo.

## What this is

Two standalone Textual TUI scripts for monitoring a **Pangolin HA Cluster** (University of Peloponnese — Digital Governance Unit). No shared package, no build step — each script is a single file, run directly with `python3`.

- `monitor.py` — live dashboard, one panel per service (Keepalived/VIP, Pangolin/Gerbil, HAProxy, Patroni/PostgreSQL, etcd, Newt agents), polls on a timer.
- `logs_viewer.py` — pulls WARN/ERROR logs over SSH from every node (`docker logs` or `journalctl`), plus resolved Newt ACCESS session logs. Has a TUI mode and a `--save` mode that writes files instead.
- `create_private_resources.py` — batch-creates Pangolin private (site) resources from a filled-in xlsx request sheet via Pangolin's **Integration API**. Each row becomes **one** site resource spanning every site in the org (via the API's `siteIds` array — the older per-site `siteId` field is deprecated), so it's reachable through any site's tunnel for HA. Not a TUI; one-shot CLI with a `--dry-run` mode. Writes a `_results_<timestamp>.xlsx` report next to the input file (adds/replaces a "Results" sheet).

Both TUI scripts read the same `config.yml` (see `config.yml.example` for the schema — node IPs/names per group, ports, SSH creds, credentials, `services:` list for the log viewer). `create_private_resources.py` reads the same file's `pangolin:` block (`base_url`, `org_slug`, `api_key`) — see the "Pangolin Integration API" section below, this is **not** the same thing as the dashboard/monitoring endpoints the other two scripts hit.

## Layout

- `monitor.py`, `logs_viewer.py`, `create_private_resources.py` — the three tools, no other source files.
- `pangolin_private_resources_template.xlsx` — the request-sheet template for `create_private_resources.py` (columns: Name, Destination, Alias, TCP Ports, UDP Ports, ICMP, User Emails, Notes; see its own instructions tab — currently in Greek, "Οδηγίες"). Filled-in request/report xlsx files are git-ignored (`*.xlsx` except `*template.xlsx`) since they carry real internal IPs/emails — don't fight that with `git add -f`.
- `config.yml.example` — schema reference; `config.yml` is the real, git-ignored, credential-bearing file.
- `requirements.txt` — pinned deps (note: `redis` is listed and `redis_timeout` exists in the config schema, but no code currently uses redis — leftover, not a bug to "fix" by wiring it up).
- No tests, no CI, no linter config.

## Pangolin Integration API (server-side, not this repo)

`create_private_resources.py` talks to Pangolin's **Integration API**, which is a separate, opt-in service — not the same backend as the dashboard (`pangolin.uop.gr/api/v1/...`, session-cookie based) or the `:3001` health endpoint the monitoring scripts use. It must be explicitly enabled per-node in Pangolin's own `config.yml` (at `/opt/pangolin/config/config.yml` inside the `pangolin` container, **not** this repo's `config.yml`):

```yaml
flags:
    enable_integration_api: true
server:
    integration_port: 3003   # default
```

...and exposed through Traefik (`/opt/pangolin/config/traefik/dynamic_config.yml`) via a dedicated router. This cluster exposes it at `https://pangolin.uop.gr/int-api` (path-based on the existing domain/cert, with a `stripPrefix` middleware) rather than the docs' example of a separate subdomain, to avoid provisioning new DNS/TLS. Hence `config.yml`'s `pangolin.base_url` here is `.../int-api`, not `.../api`.

**As of 2026-07-08, this is only applied on nodes 2 and 3 (`10.99.97.52`/`.53`). Node 1 (`10.99.97.51`) still needs the same `config.yml` + `dynamic_config.yml` change and a `pangolin`+`traefik` container restart** — it was unreachable via SSH during a genset maintenance window and was deliberately skipped rather than blocking on it. Until node 1 is updated, if the keepalived VIP fails over to node 1, `create_private_resources.py` will fail (Integration API not enabled there) even though it works fine from nodes 2/3. Check with `monitor.py` or `ssh` before assuming the fix is fully rolled out.

Each Pangolin node's `config.yml`/`dynamic_config.yml` are independent, per-node files (no shared filesystem) — any change here must be replicated to all three and containers restarted one at a time (VIP fails over to the others during each restart).

## Working in this repo

- Config-driven by design: adding/removing a node or service should never require touching the Python — check `config.yml.example` for the right schema shape before adding new fields to the scripts.
- Both scripts tolerate `nodes.newt` being either a plain list or `{ssh: {...}, hosts: [...]}` (per-group SSH override). If you touch node-group parsing, preserve that shape in both files — they duplicate this logic (see `monitor.py`'s `_newt_raw` handling and `logs_viewer.py`'s `get_nodes`/`get_ssh_creds`).
- Health checks favor inference over new dependencies where there's no clean API: Gerbil and Keepalived health are both inferred from the Pangolin API endpoint rather than adding SSH/docker-inspect checks. Follow that pattern rather than introducing a heavier check unless asked.
- `_UNICODE`/bullet handling in `monitor.py` exists because some Proxmox CTs lack a UTF-8 locale — don't remove the ASCII fallback.
- Panels in `monitor.py` are conditionally shown only when their node group is non-empty (see `if NEWT_NODES:` in `compose`/`on_mount`/`_apply_updates`) — new optional panels should follow that pattern rather than always rendering.
- `README.md` is the user-facing doc; keep it in sync when adding panels, CLI flags, or output files (e.g. the `_newt.csv` side-output from `--save` mode).
- `create_private_resources.py` always sends `roleIds: []` and grants access only via resolved `userIds` — that's deliberate (per-resource, email-based allowlists, no role-based access), not a gap to fill in.
- `create_private_resources.py` only supports creating site resources today — updating an existing one (e.g. looked up by the `niceId` shown in a prior Results sheet) is a planned TODO (see the comment above `main()`). Note the API's update/delete routes are keyed by the numeric `siteResourceId`, not `niceId` — a niceId-based `--update` mode would need to resolve niceId → siteResourceId first via `GET /org/{orgId}/site-resources`.

## Environment

Dependencies live in the `pangolin` conda env (`/home/ktsouvalis/miniconda3/envs/pangolin`), not system/user Python. Run scripts with that env's interpreter (e.g. `/home/ktsouvalis/miniconda3/envs/pangolin/bin/python3 monitor.py`, or `conda run -n pangolin python3 ...` / after `conda activate pangolin`). If a new dependency is needed, install it into that env (`conda run -n pangolin pip install <pkg>`) — never `pip install` system-wide or `--user`.

## Testing changes

No automated tests. Verify by running against a real or representative `config.yml`, using the `pangolin` conda env:
```bash
conda run -n pangolin python3 monitor.py
conda run -n pangolin python3 logs_viewer.py
conda run -n pangolin python3 logs_viewer.py --save /tmp/test_logs
conda run -n pangolin python3 create_private_resources.py pangolin_private_resources_template.xlsx --dry-run
```
`config.yml` requires live cluster network access (HTTP endpoints + SSH) to exercise fully; without it, checks will just show DOWN/UNREACHABLE, which is still useful for confirming the UI doesn't crash. `create_private_resources.py --dry-run` still needs a reachable Pangolin API + valid `api_key` (it verifies the org and lists sites/users before dry-running the row parsing).
