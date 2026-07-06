#!/usr/bin/env python3
"""
logs_viewer.py — Pangolin HA cluster log viewer (warnings + errors, last 24h)

TUI mode (default):
  Outer tabs: one per node.  Inner tabs: one per service on that node.

Save mode (--save <file>):
  Fetches logs and writes a plain-text report; no TUI shown.

Usage:
  python3 logs_viewer.py [--config pangolin_logs_config.yml]
  python3 logs_viewer.py --save cluster_logs.txt
"""
import os
import re
import sys
import argparse
import yaml
import paramiko
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, TabbedContent, TabPane, RichLog, DataTable
from textual import work
import psycopg2
import ipaddress
import csv

LOG_HOURS = 24
MAX_LINES = 500
ACCESS_LOG_HOURS = 168  # 7 days — separate window from the WARN/ERROR tabs

CSS = """\
Screen { background: $surface; }
TabbedContent { height: 1fr; }
TabPane { padding: 0; }
RichLog {
    height: 1fr;
    margin: 0 1 1 1;
    border: round $primary-darken-2;
    background: $surface-darken-1;
    scrollbar-gutter: stable;
}
"""

ACCESS_START_RE = re.compile(
    r"ACCESS START session=(?P<session>\S+) resource=(?P<resource>\d+) "
    r"proto=(?P<proto>\S+) src=(?P<src>[\d.]+):(?P<sport>\d+) "
    r"dst=(?P<dst>[\d.]+):(?P<dport>\d+) time=(?P<time>\S+)"
)
ACCESS_END_RE = re.compile(
    r"ACCESS END session=(?P<session>\S+) resource=(?P<resource>\d+) "
    r"proto=(?P<proto>\S+) src=(?P<src>[\d.]+):(?P<sport>\d+) "
    r"dst=(?P<dst>[\d.]+):(?P<dport>\d+) started=(?P<started>\S+) "
    r"ended=(?P<ended>\S+) duration=(?P<duration>\S+)"
)

def _slug(text):
    return re.sub(r"[^a-z0-9]", "-", text.lower())


def _node_tab_id(node_name):
    return f"ntab-{_slug(node_name)}"


def _svc_tab_id(node_name, label):
    return f"stab-{_slug(node_name)}-{_slug(label)}"


def _log_id(node_name, label):
    return f"log-{_slug(node_name)}-{_slug(label)}"

def _access_tab_id(node_name):
    return f"stab-{_slug(node_name)}-access"


def _access_table_id(node_name):
    return f"access-{_slug(node_name)}"


def node_has_newt(info):
    return any(label == "Newt" for label, *_ in info["services"])


def get_nodes(config, key):
    if key == "keepalived":
        return config["keepalived"]["nodes"]
    group = config["nodes"][key]
    # newt (and any future group) may be a dict with its own ssh override:
    # {"ssh": {...}, "hosts": [...]}  instead of a plain list.
    if isinstance(group, dict):
        return group["hosts"]
    return group


def get_ssh_creds(config, nodes_key):
    """Return (username, key_file) for a node group, falling back to the
    global ssh: block when the group has no override."""
    global_ssh = config["ssh"]
    if nodes_key not in ("keepalived",):
        group = config.get("nodes", {}).get(nodes_key)
        if isinstance(group, dict) and "ssh" in group:
            override = group["ssh"]
            return (
                override.get("username", global_ssh["username"]),
                override.get("key_file", global_ssh["key_file"]),
            )
    return global_ssh["username"], global_ssh["key_file"]


def load_services(config):
    """Return list of (label, nodes_key, src_type, identifier) from config['services']."""
    result = []
    for svc in config.get("services", []):
        identifier = svc.get("container") or svc.get("unit", "")
        result.append((svc["label"], svc["nodes"], svc["type"], identifier))
    return result


def build_node_map(config, services):
    """Return ordered dict: node_name -> {ip, ssh: (user, key), services: [(label, src_type, identifier)]}"""
    node_map = {}
    for label, nodes_key, src_type, identifier in services:
        creds = get_ssh_creds(config, nodes_key)
        for node in get_nodes(config, nodes_key):
            name = node["name"]
            if name not in node_map:
                node_map[name] = {"ip": node["ip"], "ssh": creds, "services": []}
            node_map[name]["services"].append((label, src_type, identifier))
    return node_map


def docker_log_cmd(container):
    return (
        f"docker logs --since {LOG_HOURS}h {container} 2>&1"
        f" | grep -iE '(WARN|WARNING|ERROR|CRITICAL|FATAL|CRIT)'"
        f" | tail -{MAX_LINES}"
    )


def systemd_log_cmd(unit):
    return (
        f"journalctl -u {unit} --since '{LOG_HOURS} hours ago'"
        f" --no-pager -p warning -o short-iso | tail -{MAX_LINES}"
    )


def ssh_run(ip, user, key_file, cmd, timeout=30):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        ip,
        username=user,
        key_filename=os.path.expanduser(key_file),
        timeout=10,
        auth_timeout=10,
    )
    try:
        _, stdout, _ = client.exec_command(cmd, timeout=timeout)
        return stdout.read().decode("utf-8", errors="replace").strip()
    finally:
        client.close()


def colorize(line):
    safe = escape(line)
    ll = line.lower()
    if any(w in ll for w in ("error", "critical", "fatal", "crit")):
        return f"[bold red]{safe}[/bold red]"
    if "warn" in ll:
        return f"[yellow]{safe}[/yellow]"
    return safe

def parse_access_sessions(raw_log: str) -> list[dict]:
    """Pair ACCESS START/END lines by session id into complete session dicts."""
    starts = {}
    sessions = []
    for line in raw_log.splitlines():
        m = ACCESS_START_RE.search(line)
        if m:
            starts[m["session"]] = m.groupdict()
            continue
        m = ACCESS_END_RE.search(line)
        if m:
            start = starts.pop(m["session"], {})
            sessions.append({
                "session": m["session"],
                "resource_id": int(m["resource"]),
                "proto": m["proto"],
                "src_ip": m["src"],
                "src_port": m["sport"],
                "dst_ip": m["dst"],
                "dst_port": m["dport"],
                "started": m["started"],
                "ended": m["ended"],
                "duration": m["duration"],
            })
    # any START without a matching END = still-open session
    for session_id, s in starts.items():
        sessions.append({
            "session": session_id,
            "resource_id": int(s["resource"]),
            "proto": s["proto"],
            "src_ip": s["src"],
            "src_port": s["sport"],
            "dst_ip": s["dst"],
            "dst_port": s["dport"],
            "started": s["time"],
            "ended": None,
            "duration": None,
        })
    return sessions


def build_lookup_maps(pg_conn) -> tuple[dict, dict, dict]:
    """Return (site_id -> site_name), (client_ip -> 'user (client)'),
    ((site_id, dst_ip, dst_port) -> resource_name) maps.
    Note: the 'resource=' field in Newt's ACCESS log lines is actually the
    Pangolin siteId, not a resources.resourceId — confirmed by matching
    Patra/Kalamata/Tripoli siteIds (35/69/36) against observed log values.
    The actual resource is identified by matching (siteId, ip, port)
    against the targets table."""
    cur = pg_conn.cursor()

    cur.execute('SELECT "siteId", name FROM sites;')
    site_map = {sid: name for sid, name in cur.fetchall()}

    cur.execute('''
        SELECT c.subnet, c.name, u.name, u.email
        FROM clients c
        LEFT JOIN "user" u ON c."userId" = u.id;
    ''')
    client_map = {}
    for subnet, client_name, user_name, user_email in cur.fetchall():
        try:
            ip = str(ipaddress.ip_interface(subnet).ip)
        except ValueError:
            continue
        who = user_name or user_email or "unknown user"
        client_map[ip] = f"{who} ({client_name})"

    cur.execute('SELECT "siteResourceId", name FROM "siteResources";')
    target_map = {sid: name for sid, name in cur.fetchall()}

    cur.close()
    return site_map, client_map, target_map

def newt_full_log_cmd():
    """Full (ungrepped) Newt log, needed to catch ACCESS START/END lines,
    which are INFO level and would be filtered out by docker_log_cmd()."""
    return f"docker logs --since {ACCESS_LOG_HOURS}h newt 2>&1"


def get_pg_connection(config):
    return psycopg2.connect(
        host=config["vip"],
        port=config["ports"]["postgres"],
        user=config["credentials"]["postgres_user"],
        password=config["credentials"]["postgres_password"],
        dbname=config["credentials"]["postgres_db"],
        connect_timeout=10,
    )


def format_session(session, site_map, client_map, target_map):
    """Turn a parsed session dict into a human-readable row."""
    who = client_map.get(session["src_ip"], session["src_ip"])
    site_name = site_map.get(session["resource_id"], f"site#{session['resource_id']}")
    resource_name = target_map.get(session["resource_id"])
    where = resource_name if resource_name else site_name
    return {
        "started": session["started"],
        "ended": session["ended"] or "ongoing",
        "duration": session["duration"] or "—",
        "who": who,
        "where": where,
        "proto": session["proto"].upper(),
        "dst": f"{session['dst_ip']}:{session['dst_port']}",
    }

# ─── Save mode ───────────────────────────────────────────────────────────────

def run_save(config, services, filename):
    node_map = build_node_map(config, services)

    tasks = [
        (node_name, info["ip"], info["ssh"], label, src_type, identifier)
        for node_name, info in node_map.items()
        for label, src_type, identifier in info["services"]
    ]

    results: dict[tuple, tuple] = {}

    def fetch_one(task):
        node_name, ip, (user, key), label, src_type, identifier = task
        cmd = docker_log_cmd(identifier) if src_type == "docker" else systemd_log_cmd(identifier)
        try:
            output = ssh_run(ip, user, key, cmd)
            return (node_name, label), output, None
        except Exception as exc:
            return (node_name, label), "", str(exc)

    total = len(tasks)
    print(f"Fetching logs from {len(node_map)} nodes ({total} service queries)…")
    with ThreadPoolExecutor(max_workers=16) as pool:
        for fut in as_completed(pool.submit(fetch_one, t) for t in tasks):
            key, output, error = fut.result()
            results[key] = (output, error)
            node_name, label = key
            if error:
                status = "SSH error"
            elif output:
                status = f"{len(output.splitlines())} lines"
            else:
                status = "clean"
            print(f"  [{len(results)}/{total}] {node_name} / {label}: {status}")

    with open(filename, "w") as f:
        f.write("Pangolin HA Cluster — Log Report\n")
        f.write(f"Fetched:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Scope:    last {LOG_HOURS}h, warnings and errors only\n")
        f.write("=" * 80 + "\n")

        for node_name, info in node_map.items():
            f.write(f"\nNODE: {node_name}  ({info['ip']})\n")
            f.write("=" * 80 + "\n")
            for label, src_type, identifier in info["services"]:
                f.write(f"\n  SERVICE: {label}  [{src_type}: {identifier}]\n")
                f.write("  " + "─" * 60 + "\n")
                output, error = results.get((node_name, label), ("", "not fetched"))
                if error:
                    f.write(f"  SSH error: {error}\n")
                elif not output:
                    f.write("  (no warnings or errors in the last 24h)\n")
                else:
                    for line in output.splitlines():
                        f.write(f"  {line}\n")

    print(f"\nSaved → {filename}")
    newt_filename = run_save_newt_access(config, node_map, filename)
    if newt_filename:
        print(f"Saved → {newt_filename}")

def run_save_newt_access(config, node_map, filename):
    """Fetch and resolve Newt ACCESS sessions; write a separate report file."""
    newt_nodes = {name: info for name, info in node_map.items() if node_has_newt(info)}
    if not newt_nodes:
        return None

    def fetch_one(item):
        node_name, info = item
        user, key = info["ssh"]
        try:
            raw = ssh_run(info["ip"], user, key, newt_full_log_cmd())
            return node_name, raw, None
        except Exception as exc:
            return node_name, "", str(exc)

    raw_logs = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed(pool.submit(fetch_one, item) for item in newt_nodes.items()):
            node_name, raw, error = fut.result()
            raw_logs[node_name] = (raw, error)

    try:
        conn = get_pg_connection(config)
        site_map, client_map, target_map = build_lookup_maps(conn)
        conn.close()
        db_error = None
    except Exception as exc:
        site_map, client_map, target_map, db_error = {}, {}, {}, str(exc)

    base, _ = os.path.splitext(filename)
    newt_filename = f"{base}_newt.csv"

    with open(newt_filename, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["Site", "Site IP", "Started", "Ended", "Duration", "Who", "Where", "Proto", "Destination"])

        for node_name, info in newt_nodes.items():
            raw, error = raw_logs.get(node_name, ("", "not fetched"))
            if error:
                writer.writerow([node_name, info["ip"], "", "", "", f"SSH error: {error}", "", "", ""])
                continue
            sessions = parse_access_sessions(raw)
            rows = [format_session(s, site_map, client_map, target_map) for s in sessions]
            rows.sort(key=lambda r: r["started"], reverse=True)
            for r in rows:
                writer.writerow([
                    node_name, info["ip"],
                    r["started"], r["ended"], r["duration"],
                    r["who"], r["where"], r["proto"], r["dst"],
                ])

    if db_error:
        print(f"WARNING: DB lookup failed ({db_error}); CSV contains raw IPs/IDs.")

    return newt_filename

# ─── TUI mode ────────────────────────────────────────────────────────────────

class LogsApp(App):
    CSS = CSS
    TITLE = "Pangolin HA – Logs (warn/error, last 24h)"
    BINDINGS = [
        ("r", "refresh_logs", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, config, services):
        super().__init__()
        self.config = config
        self._node_map = build_node_map(config, services)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent():
            for node_name, info in self._node_map.items():
                with TabPane(node_name, id=_node_tab_id(node_name)):
                    with TabbedContent():
                        for label, *_ in info["services"]:
                            with TabPane(label, id=_svc_tab_id(node_name, label)):
                                yield RichLog(
                                    id=_log_id(node_name, label),
                                    markup=True,
                                    highlight=False,
                                    wrap=True,
                                )
                        if node_has_newt(info):
                            with TabPane("Access Logs", id=_access_tab_id(node_name)):
                                yield DataTable(id=_access_table_id(node_name))
        yield Footer()

    def on_mount(self):
        for node_name, info in self._node_map.items():
            if node_has_newt(info):
                table = self.query_one(f"#{_access_table_id(node_name)}", DataTable)
                table.add_columns("Started", "Ended", "Duration", "Who", "Where", "Proto", "Destination")
        self.action_refresh_logs()

    def action_refresh_logs(self):
        for node_name, info in self._node_map.items():
            for label, *_ in info["services"]:
                rl = self.query_one(f"#{_log_id(node_name, label)}", RichLog)
                rl.clear()
                rl.write("[dim]Fetching…[/dim]")
        self._fetch_all()
        self._fetch_access_all()

    @work(thread=True, exclusive=True)
    def _fetch_access_all(self):
        newt_nodes = {
            name: info for name, info in self._node_map.items() if node_has_newt(info)
        }
        if not newt_nodes:
            return

        # Fetch raw Newt logs over SSH, one thread per node
        def fetch_one(item):
            node_name, info = item
            user, key = info["ssh"]
            try:
                raw = ssh_run(info["ip"], user, key, newt_full_log_cmd())
                return node_name, raw, None
            except Exception as exc:
                return node_name, "", str(exc)

        raw_logs = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            for fut in as_completed(pool.submit(fetch_one, item) for item in newt_nodes.items()):
                node_name, raw, error = fut.result()
                raw_logs[node_name] = (raw, error)

        # One DB connection for all lookups
        try:
            conn = get_pg_connection(self.config)
            resource_map, client_map = build_lookup_maps(conn)
            conn.close()
            db_error = None
        except Exception as exc:
            resource_map, client_map, db_error = {}, {}, str(exc)

        for node_name, (raw, error) in raw_logs.items():
            if error:
                self.call_from_thread(self._write_access_error, node_name, error)
                continue
            sessions = parse_access_sessions(raw)
            rows = [format_session(s, resource_map, client_map) for s in sessions]
            rows.sort(key=lambda r: r["started"], reverse=True)
            self.call_from_thread(self._write_access_table, node_name, rows, db_error)

    def _write_access_table(self, node_name, rows, db_error):
        table = self.query_one(f"#{_access_table_id(node_name)}", DataTable)
        table.clear()
        if db_error:
            table.add_row(f"[bold red]DB lookup failed: {escape(db_error)}[/bold red]", "", "", "", "", "", "")
            return
        if not rows:
            table.add_row("[dim]No sessions in the last 7 days[/dim]", "", "", "", "", "", "")
            return
        for r in rows:
            table.add_row(
                r["started"], r["ended"], r["duration"],
                r["who"], r["where"], r["proto"], r["dst"],
            )

    def _write_access_error(self, node_name, error):
        table = self.query_one(f"#{_access_table_id(node_name)}", DataTable)
        table.clear()
        table.add_row(f"[bold red]SSH error: {escape(error)}[/bold red]", "", "", "", "", "", "")

    def _write_service(self, node_name, label, output, error):
        rl = self.query_one(f"#{_log_id(node_name, label)}", RichLog)
        rl.clear()
        if error:
            rl.write(f"[bold red]SSH error: {escape(error)}[/bold red]")
        elif not output:
            rl.write("[dim italic](no warnings or errors in the last 24h)[/dim italic]")
        else:
            for line in output.splitlines():
                rl.write(colorize(line))

    def _mark_done(self):
        self.sub_title = f"Last fetched: {datetime.now().strftime('%H:%M:%S')}"


# ─── Entry point ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Pangolin HA cluster log viewer")
    parser.add_argument(
        "--config",
        default="config.yml",
        help="Path to config file (default: pangolin_logs_config.yml)",
    )
    parser.add_argument(
        "--save",
        metavar="FILE",
        help="Save logs to FILE as plain text (no TUI)",
    )
    parser.add_argument(
        "--last",
        type=int,
        metavar="HOURS",
        help="Override the lookback window (hours) for both WARN/ERROR and Newt access logs",
    )
    args = parser.parse_args()

    if args.last is not None:
        global LOG_HOURS, ACCESS_LOG_HOURS
        LOG_HOURS = args.last
        ACCESS_LOG_HOURS = args.last

    with open(args.config) as f:
        config = yaml.safe_load(f)

    services = load_services(config)
    if not services:
        print(
            "Error: no services defined in config. Add a 'services:' section.",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.save:
        save_path = args.save if args.save.endswith(".log") else args.save + ".log"
        run_save(config, services, save_path)
    else:
        LogsApp(config, services).run()


if __name__ == "__main__":
    main()