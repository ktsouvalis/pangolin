# CLAUDE.md

Guidance for working in this repo.

## What this is

Two standalone Textual TUI scripts for monitoring a **Pangolin HA Cluster** (University of Peloponnese — Digital Governance Unit). No shared package, no build step — each script is a single file, run directly with `python3`.

- `monitor.py` — live dashboard, one panel per service (Keepalived/VIP, Pangolin/Gerbil, HAProxy, Patroni/PostgreSQL, etcd, Newt agents), polls on a timer.
- `logs_viewer.py` — pulls WARN/ERROR logs over SSH from every node (`docker logs` or `journalctl`), plus resolved Newt ACCESS session logs. Has a TUI mode and a `--save` mode that writes files instead.

Both scripts read the same `config.yml` (see `config.yml.example` for the schema — node IPs/names per group, ports, SSH creds, credentials, `services:` list for the log viewer).

## Layout

- `monitor.py`, `logs_viewer.py` — the two tools, no other source files.
- `config.yml.example` — schema reference; `config.yml` is the real, git-ignored, credential-bearing file.
- `requirements.txt` — pinned deps (note: `redis` is listed and `redis_timeout` exists in the config schema, but no code currently uses redis — leftover, not a bug to "fix" by wiring it up).
- No tests, no CI, no linter config.

## Working in this repo

- Config-driven by design: adding/removing a node or service should never require touching the Python — check `config.yml.example` for the right schema shape before adding new fields to the scripts.
- Both scripts tolerate `nodes.newt` being either a plain list or `{ssh: {...}, hosts: [...]}` (per-group SSH override). If you touch node-group parsing, preserve that shape in both files — they duplicate this logic (see `monitor.py`'s `_newt_raw` handling and `logs_viewer.py`'s `get_nodes`/`get_ssh_creds`).
- Health checks favor inference over new dependencies where there's no clean API: Gerbil and Keepalived health are both inferred from the Pangolin API endpoint rather than adding SSH/docker-inspect checks. Follow that pattern rather than introducing a heavier check unless asked.
- `_UNICODE`/bullet handling in `monitor.py` exists because some Proxmox CTs lack a UTF-8 locale — don't remove the ASCII fallback.
- Panels in `monitor.py` are conditionally shown only when their node group is non-empty (see `if NEWT_NODES:` in `compose`/`on_mount`/`_apply_updates`) — new optional panels should follow that pattern rather than always rendering.
- `README.md` is the user-facing doc; keep it in sync when adding panels, CLI flags, or output files (e.g. the `_newt.csv` side-output from `--save` mode).

## Testing changes

No automated tests. Verify by running against a real or representative `config.yml`:
```bash
python3 monitor.py
python3 logs_viewer.py
python3 logs_viewer.py --save /tmp/test_logs
```
`config.yml` requires live cluster network access (HTTP endpoints + SSH) to exercise fully; without it, checks will just show DOWN/UNREACHABLE, which is still useful for confirming the UI doesn't crash.
