#!/usr/bin/env python3
"""
monitor.py — Pangolin HA Cluster Monitor
-----------------------------------------
University of Peloponnese — Digital Governance Unit

Real-time TUI dashboard for the full Pangolin HA stack.
All connection details read from config.yml (or a path passed as first argument).

Usage:
    python3 monitor.py                   # uses config.yml in current dir
    python3 monitor.py config.yml        # explicit config path
"""

import re
import sys
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Optional

try:
    import psycopg2
    _HAS_PSYCOPG2 = True
except ImportError:
    _HAS_PSYCOPG2 = False

import yaml
import requests
import urllib3

from textual.app import App, ComposeResult
from textual.widgets import Static, Footer
from textual.reactive import reactive
from textual import work
from textual.containers import Horizontal

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ---------------------------------------------------------------------------
# Load config
# ---------------------------------------------------------------------------

def load_config(path: str = "config.yml") -> dict:
    if not os.path.exists(path):
        print(f"[ERROR] Config file not found: {path}")
        print(f"        Copy config.yml.example to {path} and fill in your values.")
        sys.exit(1)
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG_PATH = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
CFG = load_config(CONFIG_PATH)

SITE_NAME        = CFG.get("site_name", "Pangolin HA Cluster")
REFRESH_INTERVAL = int(CFG.get("refresh_interval", 20))
HTTP_TIMEOUT     = int(CFG.get("http_timeout", 5))
VIP              = CFG.get("vip", "")

NODES  = CFG.get("nodes", {})
PORTS  = CFG.get("ports", {})
CREDS  = CFG.get("credentials", {})
KA_CFG = CFG.get("keepalived", {})

PANGOLIN_NODES = NODES.get("pangolin", [])
PATRONI_NODES  = NODES.get("patroni",  [])
ETCD_NODES     = NODES.get("etcd",     [])
HAPROXY_NODES  = NODES.get("haproxy",  [])
KA_NODES       = KA_CFG.get("nodes",   [])
TRACK_WEIGHT   = int(KA_CFG.get("track_weight", -25))

P_PANGOLIN = int(PORTS.get("pangolin",        3001))
P_PATRONI  = int(PORTS.get("patroni",         8008))
P_HAPROXY  = int(PORTS.get("haproxy_stats",   9000))
P_ETCD     = int(PORTS.get("etcd",            2379))
P_POSTGRES = int(PORTS.get("postgres",        5432))

PG_USER  = CREDS.get("postgres_user",     "postgres")
PG_PASS  = CREDS.get("postgres_password", "")
PG_DB    = CREDS.get("postgres_db",       "pangolin")

# ---------------------------------------------------------------------------
# Check functions
# ---------------------------------------------------------------------------

def check_vip_holder() -> dict:
    try:
        r = requests.get(f"http://{VIP}:{P_PANGOLIN}/api/v1/",
                         timeout=HTTP_TIMEOUT)
        reachable = r.status_code in (200, 401, 403)
    except Exception:
        reachable = False
    return {"reachable": reachable}


def check_keepalived_node(node: dict) -> dict:
    ip   = node["ip"]
    name = node.get("name", ip)
    base = int(node.get("base_priority", 100))
    # infer keepalived health from Pangolin API reachability on this node
    try:
        r = requests.get(f"http://{ip}:{P_PANGOLIN}/api/v1/",
                         timeout=HTTP_TIMEOUT)
        pangolin_up = r.status_code in (200, 401, 403)
    except Exception:
        pangolin_up = False
    effective = base if pangolin_up else base + TRACK_WEIGHT
    return {
        "ip": ip, "name": name,
        "pangolin_up": pangolin_up,
        "base_priority": base,
        "effective_priority": effective,
    }


def check_pangolin_node(node: dict) -> dict:
    ip   = node["ip"]
    name = node.get("name", ip)
    # API health
    try:
        r = requests.get(f"http://{ip}:{P_PANGOLIN}/api/v1/",
                         timeout=HTTP_TIMEOUT)
        api_ok = r.status_code in (200, 401, 403)
    except Exception:
        api_ok = False
    return {"ip": ip, "name": name, "api_ok": api_ok}


def check_gerbil_node(node: dict) -> dict:
    """
    Gerbil has no HTTP health endpoint. We infer health from Pangolin:
    if Pangolin is healthy, Gerbil is assumed to be running (they share
    the same compose stack and Pangolin depends on Gerbil).
    For a more accurate check, SSH + docker inspect would be needed.
    """
    ip   = node["ip"]
    name = node.get("name", ip)
    try:
        r = requests.get(f"http://{ip}:{P_PANGOLIN}/api/v1/",
                         timeout=HTTP_TIMEOUT)
        inferred_ok = r.status_code in (200, 401, 403)
    except Exception:
        inferred_ok = False
    return {"ip": ip, "name": name, "inferred_ok": inferred_ok}


def check_patroni_node(node: dict) -> dict:
    ip   = node["ip"]
    name = node.get("name", ip)
    try:
        r    = requests.get(f"http://{ip}:{P_PATRONI}/", timeout=HTTP_TIMEOUT)
        data = r.json()
        raw_role  = data.get("role", "unknown")
        is_leader = raw_role in ("primary", "master", "standby_leader")
        role      = "primary" if is_leader else "replica"
        tl        = data.get("timeline")
        tl_str    = str(tl) if tl is not None else "—"
        repl_state = data.get("replication_state", "")
        state      = repl_state if repl_state else data.get("state", "unknown")
        lag_bytes  = None
        if not is_leader:
            xlog     = data.get("xlog", {})
            received = xlog.get("received_location")
            replayed = xlog.get("replayed_location")
            if received is not None and replayed is not None:
                lag_bytes = max(0, received - replayed)
        return {
            "ip": ip, "name": name, "ok": True,
            "role": role, "state": state, "timeline": tl_str,
            "pending_restart": data.get("pending_restart", False),
            "lag_bytes": lag_bytes,
        }
    except Exception:
        return {
            "ip": ip, "name": name, "ok": False,
            "role": "down", "state": "unreachable",
            "timeline": "—", "pending_restart": False, "lag_bytes": None,
        }


def check_patroni_history(ip: str) -> dict:
    try:
        r = requests.get(f"http://{ip}:{P_PATRONI}/history", timeout=HTTP_TIMEOUT)
        entries = r.json()
        if not entries:
            return {}
        last = entries[-1]
        return {
            "timeline":  last[0] if len(last) > 0 else "?",
            "reason":    last[2] if len(last) > 2 else "unknown",
            "timestamp": last[3] if len(last) > 3 else None,
        }
    except Exception:
        return {}


def check_etcd_node(node: dict) -> dict:
    ip   = node["ip"]
    name = node.get("name", ip)
    try:
        r = requests.get(f"http://{ip}:{P_ETCD}/health", timeout=HTTP_TIMEOUT)
        healthy = r.json().get("health") in (True, "true")
    except Exception:
        return {"ip": ip, "name": name, "ok": False,
                "leader": False, "raft_term": "?", "db_kb": 0}
    is_leader = False
    raft_term = "?"
    db_kb     = 0
    try:
        r2 = requests.post(
            f"http://{ip}:{P_ETCD}/v3/maintenance/status",
            json={}, timeout=HTTP_TIMEOUT,
        )
        d2        = r2.json()
        member_id = d2.get("header", {}).get("member_id", "")
        leader_id = d2.get("leader", "")
        is_leader = bool(member_id and leader_id and member_id == leader_id)
        raft_term = d2.get("raftTerm", "?")
        db_kb     = int(d2.get("dbSizeInUse", 0)) // 1024
    except Exception:
        pass
    return {
        "ip": ip, "name": name, "ok": healthy,
        "leader": is_leader, "raft_term": raft_term, "db_kb": db_kb,
    }


def check_haproxy_node(node: dict) -> dict:
    ip   = node["ip"]
    name = node.get("name", ip)
    try:
        r = requests.get(
            f"http://{ip}:{P_HAPROXY}/stats;csv",
            timeout=HTTP_TIMEOUT,
        )
        if r.status_code != 200:
            return {"ip": ip, "name": name, "ok": False, "backends": {}}
        backends      = {}
        backend_stats = {}
        for line in r.text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.split(",")
            if len(parts) < 18:
                continue
            pxname, svname, status = parts[0], parts[1], parts[17]
            if svname == "FRONTEND":
                continue
            if svname == "BACKEND":
                def _int(col):
                    return int(parts[col]) if len(parts) > col and parts[col].strip().isdigit() else 0
                backend_stats[pxname] = {"rate": _int(33), "errs": _int(43)}
                continue
            backends.setdefault(pxname, []).append({"server": svname, "status": status})
        return {"ip": ip, "name": name, "ok": True,
                "backends": backends, "backend_stats": backend_stats}
    except Exception:
        return {"ip": ip, "name": name, "ok": False, "backends": {}}


def check_postgres_node(node: dict) -> dict:
    """Connect directly to PostgreSQL port 5432 on each node.
    HAProxy binds on 127.0.0.1:5000 (localhost only) so direct port is used."""
    if not _HAS_PSYCOPG2 or not PG_PASS:
        return {"ip": node["ip"], "name": node.get("name", node["ip"]),
                "ok": None, "error": "psycopg2 not installed or no password"}
    ip   = node["ip"]
    name = node.get("name", ip)
    try:
        conn = psycopg2.connect(
            host=ip, port=P_POSTGRES,
            user=PG_USER, password=PG_PASS, dbname=PG_DB,
            connect_timeout=HTTP_TIMEOUT,
        )
        cur = conn.cursor()
        cur.execute("SELECT pg_is_in_recovery();")
        is_replica = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {"ip": ip, "name": name, "ok": True, "is_replica": is_replica}
    except Exception as e:
        return {"ip": ip, "name": name, "ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Dot indicators
# ---------------------------------------------------------------------------

def _terminal_supports_unicode() -> bool:
    if CFG.get("unicode_bullets") is not None:
        return bool(CFG["unicode_bullets"])
    import locale
    for var in ("LC_ALL", "LC_CTYPE", "LANG"):
        val = os.environ.get(var, "")
        if val and "utf" in val.lower():
            return True
    try:
        if "utf" in (locale.getpreferredencoding(False) or "").lower():
            return True
    except Exception:
        pass
    return False

_UNICODE = _terminal_supports_unicode()
_BULLET  = "●" if _UNICODE else "*"

OK   = f"[bold green]{_BULLET}[/]"
DOWN = f"[bold red]{_BULLET}[/]"
WARN = f"[bold yellow]{_BULLET}[/]"
GREY = f"[dim white]{_BULLET}[/]"


def failures_to_dot(failures: int) -> str:
    if failures >= 3: return DOWN
    if failures >= 2: return WARN
    return OK


_MB = 1024 * 1024
PATRONI_LAG_WARN = 1   * _MB
PATRONI_LAG_CRIT = 100 * _MB


def _fmt_lag(lag_bytes: Optional[int],
             warn_bytes: int = PATRONI_LAG_WARN,
             crit_bytes: int = PATRONI_LAG_CRIT) -> str:
    if lag_bytes is None:
        return ""
    if lag_bytes < 1024:
        val_str = f"{lag_bytes}B"
    elif lag_bytes < _MB:
        val_str = f"{lag_bytes // 1024}KB"
    else:
        val_str = f"{lag_bytes // _MB}MB"
    if lag_bytes >= crit_bytes:
        color = "bold red"
    elif lag_bytes >= warn_bytes:
        color = "yellow"
    else:
        color = "cyan"
    return f"  lag=[{color}]{val_str}[/]"


# ---------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------

class KeepalivedPanel(Static):
    data: reactive[dict] = reactive({})

    def render_content(self) -> str:
        d = self.data
        if not d:
            return "  [dim]Checking...[/]"

        vip_d     = d.get("vip", {})
        nodes     = d.get("nodes", [])
        reachable = vip_d.get("reachable", False)

        # Find the master node (highest effective priority among pangolin_up nodes)
        up_nodes = [n for n in nodes if n["pangolin_up"]]
        master   = max(up_nodes, key=lambda n: n["effective_priority"]) if up_nodes else None
        holder_name = master["name"] if master else None

        vip_str = (
            f"{OK} VIP [bold cyan]{VIP}[/]  →  MASTER: [bold green]{holder_name}[/]"
            if reachable
            else f"{DOWN} VIP [bold cyan]{VIP}[/]  →  [bold red]UNREACHABLE[/]"
        )

        lines = [f"  {vip_str}", ""]

        for node in nodes:
            name   = node["name"]
            up     = node["pangolin_up"]
            base   = node["base_priority"]
            eff    = node["effective_priority"]
            is_master = master and master["name"] == name

            if is_master:
                state_str = "[bold green]MASTER[/]"
                d_dot     = OK
                name_fmt  = f"[bold green]{name:<18}[/]"
            elif up:
                state_str = "[dim white]BACKUP[/]"
                d_dot     = GREY
                name_fmt  = f"[dim white]{name:<18}[/]"
            else:
                state_str = "[bold red]FAULT[/]"
                d_dot     = DOWN
                name_fmt  = f"[bold red]{name:<18}[/]"

            prio_str = f"priority=[cyan]{eff}[/]"
            if not up:
                prio_str += f" [dim white](base {base} {TRACK_WEIGHT})[/]"

            lines.append(f"  {d_dot} {name_fmt} {state_str}  {prio_str}")

        return "\n".join(lines)

    def watch_data(self, data: dict) -> None:
        self.update(self.render_content())


class PangolinPanel(Static):
    data: reactive[list] = reactive([])

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        lines = []
        for node in self.data:
            name   = node["name"]
            api_ok = node.get("api_ok", False)
            g_ok   = node.get("inferred_ok", False)

            api_str = f"{OK} [green]API[/]"    if api_ok else f"{DOWN} [red]API[/]"
            g_str   = f"{OK} [green]gerbil[/]" if g_ok   else f"{DOWN} [red]gerbil[/]"
            overall = OK if (api_ok and g_ok) else (WARN if (api_ok or g_ok) else DOWN)
            color   = "green" if (api_ok and g_ok) else ("yellow" if (api_ok or g_ok) else "red")
            lines.append(f"  {overall} [{color}]{name:<18}[/] {api_str}   {g_str}")
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class PatroniPanel(Static):
    data: reactive[dict] = reactive({})

    def render_content(self) -> str:
        d = self.data
        if not d or "nodes" not in d:
            return "  [dim]Checking...[/]"

        lines = []
        for node in d["nodes"]:
            name = node["name"]
            if not node["ok"]:
                lines.append(f"  {DOWN} [bold red]{name:<18}[/] [red]UNREACHABLE[/]")
                continue
            role      = node["role"]
            state     = node["state"]
            tl        = node["timeline"]
            pend      = " [yellow](restart pending)[/]" if node.get("pending_restart") else ""
            is_leader = role in ("primary", "master")
            d_dot     = OK if is_leader else GREY
            role_str  = "[bold green]LEADER[/]" if is_leader else "[dim white]REPLICA[/]"
            nfmt      = f"[bold green]{name:<18}[/]" if is_leader else f"[dim white]{name:<18}[/]"
            lag_val   = node.get("lag_bytes")
            lag_str   = "" if is_leader else (_fmt_lag(lag_val) if lag_val is not None else "")
            lines.append(
                f"  {d_dot} {nfmt} {role_str}  "
                f"state=[cyan]{state}[/]  TL=[cyan]{tl}[/]{lag_str}{pend}"
            )
        return "\n".join(lines)

    def watch_data(self, data: dict) -> None:
        self.update(self.render_content())


class EtcdPanel(Static):
    data: reactive[list] = reactive([])

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        lines = []
        for node in self.data:
            name  = node["name"]
            if not node["ok"]:
                lines.append(f"  {DOWN} [bold red]{name:<18}[/] [red]UNREACHABLE[/]")
                continue
            term  = node.get("raft_term", "?")
            db_kb = node.get("db_kb", 0)
            if node["leader"]:
                lines.append(
                    f"  {OK} [bold green]{name:<18}[/] [bold green]LEADER[/]  "
                    f"term=[cyan]{term}[/]  db=[cyan]{db_kb}KB[/]"
                )
            else:
                lines.append(
                    f"  {GREY} [dim white]{name:<18}[/] [dim white]FOLLOWER[/]  "
                    f"term=[cyan]{term}[/]  db=[cyan]{db_kb}KB[/]"
                )
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class HAProxyPanel(Static):
    data: reactive[list] = reactive([])

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        lines = []
        for node in self.data:
            name = node["name"]
            if not node["ok"]:
                lines.append(f"  {DOWN} [bold red]{name:<18}[/] [red]STATS UNREACHABLE[/]")
                continue
            backends      = node.get("backends", {})
            backend_stats = node.get("backend_stats", {})
            any_zero      = False
            parts         = []
            for pxname, servers in backends.items():
                ups   = sum(1 for s in servers if s["status"] == "UP")
                total = len(servers)
                if ups == 0:
                    any_zero = True
                stats   = backend_stats.get(pxname, {})
                errs    = stats.get("errs", 0)
                rate    = stats.get("rate", 0)
                err_str = f"[red]5xx:{errs}[/]" if errs > 0 else "[dim]5xx:0[/]"
                parts.append(f"[cyan]{pxname}[/]: {ups}/{total} {rate}/s {err_str}")
            summary = "  ".join(parts) if parts else "[dim]no backends[/]"
            # Use WARN (yellow) when a backend has 0 UP servers — the HAProxy
            # node itself is reachable so DOWN would be misleading. In a
            # Patroni setup, partial UP counts are role-based and expected.
            d_dot, color = (WARN, "yellow") if any_zero else (OK, "green")
            lines.append(f"  {d_dot} [bold {color}]{name:<18}[/] {summary}")
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class PostgreSQLPanel(Static):
    data: reactive[list] = reactive([])

    def render_content(self) -> str:
        if not self.data:
            return "  [dim]Checking...[/]"
        if not _HAS_PSYCOPG2 or not PG_PASS:
            return f"  {GREY} [dim]psycopg2 not installed or no password configured[/]"
        lines = []
        for node in self.data:
            name = node["name"]
            if node.get("ok") is None:
                lines.append(f"  {GREY} [dim white]{name:<18}[/] [dim]skipped[/]")
                continue
            if not node["ok"]:
                err = str(node.get("error", ""))[:50]
                lines.append(f"  {DOWN} [bold red]{name:<18}[/] [red]FAILED[/]  [dim]{err}[/]")
                continue
            is_replica = node.get("is_replica", True)
            role_str   = "[dim white]REPLICA[/]" if is_replica else "[bold green]PRIMARY[/]"
            d_dot      = GREY if is_replica else OK
            nfmt       = f"[bold green]{name:<18}[/]" if not is_replica else f"[dim white]{name:<18}[/]"
            lines.append(f"  {d_dot} {nfmt} {role_str}")
        return "\n".join(lines)

    def watch_data(self, data: list) -> None:
        self.update(self.render_content())


class StatusBar(Static):
    last_refresh: reactive[str] = reactive("")
    status_dot:   reactive[str] = reactive(GREY)

    def render_content(self) -> str:
        ts = self.last_refresh or "—"
        return (
            f"  {self.status_dot}    "
            f"[dim]Last refresh: {ts}   "
            f"Auto-refresh: {REFRESH_INTERVAL}s   "
            f"Config: {CONFIG_PATH}[/]"
        )

    def watch_last_refresh(self, _: str) -> None:
        self.update(self.render_content())

    def watch_status_dot(self, _: str) -> None:
        self.update(self.render_content())


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

CSS = """
Screen {
    background: #0d1117;
    color: #e6edf3;
}

#title {
    content-align: center middle;
    background: #161b22;
    color: #58a6ff;
    text-style: bold;
    height: 1;
    padding: 0 2;
}

#statusbar {
    height: 1;
    content-align: center middle;
    padding: 0 2;
    margin-bottom: 1;
}

.panel {
    border: solid #30363d;
    border-title-color: #58a6ff;
    border-title-style: bold;
    padding: 0 1;
    margin: 0 1 1 1;
    height: auto;
    background: #161b22;
    width: 1fr;
}

Footer {
    background: #161b22;
    color: #8b949e;
}
"""

# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class ClusterMonitor(App):
    CSS = CSS
    TITLE = SITE_NAME
    BINDINGS = [
        ("r", "refresh_now", "Refresh"),
        ("q", "quit",        "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(f"  {GREY}  {SITE_NAME}", id="title")
        yield StatusBar(id="statusbar")

        # Row 1: VIP/Keepalived (full width)
        yield KeepalivedPanel("  [dim]Checking...[/]",
                              id="panel-keepalived", classes="panel")

        # Row 2: Pangolin backends (full width)
        yield PangolinPanel("  [dim]Checking...[/]",
                            id="panel-pangolin", classes="panel")

        # Row 3: HAProxy (full width)
        yield HAProxyPanel("  [dim]Checking...[/]",
                           id="panel-haproxy", classes="panel")

        # Row 4: Patroni + etcd side by side
        with Horizontal(id="row-db"):
            yield PatroniPanel("  [dim]Checking...[/]",
                               id="panel-patroni", classes="panel")
            yield EtcdPanel("  [dim]Checking...[/]",
                            id="panel-etcd", classes="panel")

        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#panel-keepalived").border_title = f" {GREY}  VIP / KEEPALIVED  "
        self.query_one("#panel-pangolin").border_title   = f" {GREY}  PANGOLIN / GERBIL BACKENDS  "
        self.query_one("#panel-haproxy").border_title    = f" {GREY}  HAPROXY BACKENDS  "
        self.query_one("#panel-patroni").border_title    = f" {GREY}  POSTGRESQL / PATRONI  "
        self.query_one("#panel-etcd").border_title       = f" {GREY}  ETCD CLUSTER  "

        self.set_interval(REFRESH_INTERVAL, self.action_refresh_now)
        self.action_refresh_now()

    @work(thread=True)
    def action_refresh_now(self) -> None:
        with ThreadPoolExecutor(max_workers=20) as ex:
            f_vip      = ex.submit(check_vip_holder)
            f_ka       = [ex.submit(check_keepalived_node, n) for n in KA_NODES]
            f_pangolin = [ex.submit(check_pangolin_node,   n) for n in PANGOLIN_NODES]
            f_gerbil   = [ex.submit(check_gerbil_node,     n) for n in PANGOLIN_NODES]
            f_patroni  = [ex.submit(check_patroni_node,    n) for n in PATRONI_NODES]
            f_etcd     = [ex.submit(check_etcd_node,       n) for n in ETCD_NODES]
            f_haproxy  = [ex.submit(check_haproxy_node,    n) for n in HAPROXY_NODES]

            vip_data      = f_vip.result()
            ka_data       = [f.result() for f in f_ka]
            pangolin_data = [f.result() for f in f_pangolin]
            gerbil_data   = [f.result() for f in f_gerbil]
            patroni_data  = [f.result() for f in f_patroni]
            etcd_data     = [f.result() for f in f_etcd]
            haproxy_data  = [f.result() for f in f_haproxy]

            # Patroni history from the current leader
            primary = next(
                (n for n in patroni_data if n["ok"] and n["role"] in ("primary", "master")),
                None,
            )
            primary_ip   = primary["ip"] if primary else None
            history_data = check_patroni_history(primary_ip) if primary_ip else {}

        # Merge pangolin + gerbil data per node
        combined_pangolin = []
        for p, g in zip(pangolin_data, gerbil_data):
            combined_pangolin.append({**p, "inferred_ok": g["inferred_ok"]})

        ts = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(
            self._apply_updates,
            vip_data, ka_data, combined_pangolin,
            patroni_data, history_data,
            etcd_data, haproxy_data, ts,
        )

    def _apply_updates(
        self,
        vip_data, ka_data, pangolin_data,
        patroni_data, history_data,
        etcd_data, haproxy_data, ts,
    ):
        self.query_one("#panel-keepalived", KeepalivedPanel).data = {
            "vip": vip_data, "nodes": ka_data,
        }
        self.query_one("#panel-pangolin",  PangolinPanel).data  = pangolin_data
        self.query_one("#panel-patroni",   PatroniPanel).data   = {
            "nodes": patroni_data, "history": history_data,
        }
        self.query_one("#panel-etcd",      EtcdPanel).data      = etcd_data
        self.query_one("#panel-haproxy",   HAProxyPanel).data   = haproxy_data

        # --- failure counts ---
        ka_fail       = sum(1 for n in ka_data       if not n["pangolin_up"])
        pangolin_fail = sum(1 for n in pangolin_data  if not n["api_ok"])
        patroni_fail  = sum(1 for n in patroni_data   if not n["ok"])
        etcd_fail     = sum(1 for n in etcd_data      if not n["ok"])
        haproxy_fail  = sum(1 for n in haproxy_data   if not n["ok"])

        # --- border title dots ---
        self.query_one("#panel-keepalived").border_title = (
            f" {failures_to_dot(ka_fail)}  VIP / KEEPALIVED  "
        )
        self.query_one("#panel-pangolin").border_title = (
            f" {failures_to_dot(pangolin_fail)}  PANGOLIN / GERBIL BACKENDS  "
        )
        self.query_one("#panel-haproxy").border_title = (
            f" {failures_to_dot(haproxy_fail)}  HAPROXY BACKENDS  "
        )

        cluster_tl = next(
            (n["timeline"] for n in patroni_data if n["ok"] and n["role"] in ("primary", "master")),
            None,
        )
        tl_suffix = f"  TL={cluster_tl}" if cluster_tl else ""
        if history_data:
            raw_ts    = history_data.get("timestamp") or ""
            try:
                ts_display = datetime.fromisoformat(raw_ts).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_display = raw_ts.replace("T", " ")[:16] if raw_ts else "unknown time"
            hist_tl     = history_data.get("timeline", "?")
            hist_reason = str(history_data.get("reason", "unknown"))[:40]
            failover_part = f"  (last failover TL {hist_tl}  {ts_display}  →  {hist_reason})"
        else:
            failover_part = ""
        self.query_one("#panel-patroni").border_title = (
            f" {failures_to_dot(patroni_fail)}  POSTGRESQL / PATRONI{tl_suffix}{failover_part}  "
        )
        self.query_one("#panel-etcd").border_title = (
            f" {failures_to_dot(etcd_fail)}  ETCD CLUSTER  "
        )

        # --- central dot ---
        all_failures = [ka_fail, pangolin_fail, patroni_fail, etcd_fail, haproxy_fail]
        if any(f >= 3 for f in all_failures):
            central_dot = DOWN
        elif any(f >= 2 for f in all_failures):
            central_dot = WARN
        else:
            central_dot = OK

        self.query_one("#title").update(f"  {central_dot}  {SITE_NAME}")

        sb = self.query_one("#statusbar", StatusBar)
        sb.status_dot   = central_dot
        sb.last_refresh = ts


if __name__ == "__main__":
    ClusterMonitor().run()