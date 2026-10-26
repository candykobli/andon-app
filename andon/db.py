"""Database layer. Works on any PostgreSQL host (Supabase, Neon, Azure, AWS RDS, on-premise) and on SQLite.

All timestamps are stored as naive UTC.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone

import pandas as pd
from sqlalchemy import (UniqueConstraint, Boolean, Column, DateTime, Float, ForeignKey, Integer, MetaData, String, Table, Text,
                        create_engine, func, insert, select, update)
from sqlalchemy.engine import Engine

metadata = MetaData()

lines = Table("andon_lines", metadata,
              Column("id", Integer, primary_key=True, autoincrement=True),
              Column("name", String(80), nullable=False),
              Column("area", String(80), nullable=False, default="Production"),
              Column("sort", Integer, nullable=False, default=0),
              Column("active", Boolean, nullable=False, default=True),
              Column("scheduled_shifts", String(200), nullable=True),   # JSON list of shift names; empty = all shifts
              Column("weekends", Boolean, nullable=False, default=False))  # produces on Saturday/Sunday

stations = Table("andon_stations", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("line_id", Integer, ForeignKey("andon_lines.id"), nullable=False),
                 Column("name", String(80), nullable=False),
                 Column("sort", Integer, nullable=False, default=0),
                 Column("active", Boolean, nullable=False, default=True),
                 Column("process", String(40), nullable=True),      # production, mixing, filling, packaging, ...
                 Column("is_output", Boolean, nullable=False, default=False))  # counts the line's output for OEE

call_types = Table("andon_call_types", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("name", String(60), nullable=False),
                   Column("color", String(9), nullable=False, default="#2563eb"),
                   Column("l1_role", String(60), nullable=False),
                   Column("l2_min", Integer, nullable=True),
                   Column("l2_role", String(60), nullable=True),
                   Column("l3_min", Integer, nullable=True),
                   Column("l3_role", String(60), nullable=True),
                   Column("sort", Integer, nullable=False, default=0),
                   Column("active", Boolean, nullable=False, default=True))

reasons = Table("andon_reasons", metadata,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("call_type_id", Integer, ForeignKey("andon_call_types.id"), nullable=False),
                Column("name", String(120), nullable=False),
                Column("active", Boolean, nullable=False, default=True))

responders = Table("andon_responders", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("name", String(80), nullable=False),
                   Column("role", String(60), nullable=False),
                   Column("pin_hash", String(128), nullable=False),
                   Column("is_admin", Boolean, nullable=False, default=False),
                   Column("active", Boolean, nullable=False, default=True),
                   # access management (the table holds every user, not only responders)
                   Column("email", String(120), nullable=True),
                   Column("level", String(20), nullable=False, default="responder"),
                   Column("status", String(12), nullable=False, default="active"),  # pending | active | rejected | disabled
                   Column("lines", String(200), nullable=True),     # JSON list of line ids; empty = all lines
                   Column("request_note", Text, nullable=True),
                   Column("requested_at", DateTime, nullable=True),
                   Column("decided_by", String(80), nullable=True),
                   Column("decided_at", DateTime, nullable=True),
                   Column("pin_clash", Boolean, nullable=False, default=False))  # requested PIN already used (not shown to requester)

calls = Table("andon_calls", metadata,
              Column("id", Integer, primary_key=True, autoincrement=True),
              Column("line_id", Integer, ForeignKey("andon_lines.id"), nullable=False),
              Column("station_id", Integer, ForeignKey("andon_stations.id"), nullable=False),
              Column("call_type_id", Integer, ForeignKey("andon_call_types.id"), nullable=False),
              Column("status", String(12), nullable=False, default="open"),  # open | ack | resolved | cancelled
              Column("raised_at", DateTime, nullable=False),
              Column("stopped_at", DateTime, nullable=True),  # set when the line is stopped (red)
              Column("ack_at", DateTime, nullable=True),
              Column("ack_by", String(80), nullable=True),
              Column("resolved_at", DateTime, nullable=True),
              Column("resolved_by", String(80), nullable=True),
              Column("reason_id", Integer, ForeignKey("andon_reasons.id"), nullable=True),
              Column("note", Text, nullable=True),
              Column("resolution_note", Text, nullable=True),
              Column("source", String(12), nullable=False, default="manual"),  # manual | auto
              Column("rule_id", Integer, nullable=True),
              Column("event_id", Integer, nullable=True))        # the exact problem, from the event catalogue

# ---- machine signals -------------------------------------------------------
signal_defs = Table("andon_signal_defs", metadata,
                    Column("id", Integer, primary_key=True, autoincrement=True),
                    Column("station_id", Integer, ForeignKey("andon_stations.id"), nullable=False),
                    Column("key", String(40), nullable=False),        # state, speed, good, scrap, temperature, ...
                    Column("label", String(60), nullable=False),
                    Column("kind", String(12), nullable=False),       # state | rate | counter | process
                    Column("unit", String(16), nullable=True),
                    Column("lo", Float, nullable=True),
                    Column("hi", Float, nullable=True),
                    Column("target", Float, nullable=True),
                    Column("sort", Integer, nullable=False, default=0),
                    Column("active", Boolean, nullable=False, default=True))

signal_latest = Table("andon_signal_latest", metadata,
                      Column("station_id", Integer, primary_key=True),
                      Column("key", String(40), primary_key=True),
                      Column("num", Float, nullable=True),
                      Column("text", String(80), nullable=True),
                      Column("ts", DateTime, nullable=False),          # last reading
                      Column("changed_at", DateTime, nullable=False),  # last time the value changed
                      Column("hist_at", DateTime, nullable=True))      # last time it was written to history

signal_history = Table("andon_signal_history", metadata,
                       Column("id", Integer, primary_key=True, autoincrement=True),
                       Column("station_id", Integer, nullable=False, index=True),
                       Column("key", String(40), nullable=False),
                       Column("num", Float, nullable=True),
                       Column("text", String(80), nullable=True),
                       Column("ts", DateTime, nullable=False, index=True))

rules = Table("andon_rules", metadata,
              Column("id", Integer, primary_key=True, autoincrement=True),
              Column("name", String(80), nullable=False),
              Column("signal_key", String(40), nullable=False),
              Column("condition", String(20), nullable=False),  # state_is | outside_limits | above_hi | below_lo | below_target_pct
              Column("param", String(80), nullable=True),       # e.g. "Fault" or "60" (percent)
              Column("duration_s", Integer, nullable=False, default=30),
              Column("call_type_id", Integer, ForeignKey("andon_call_types.id"), nullable=False),
              Column("line_stopped", Boolean, nullable=False, default=False),
              Column("station_id", Integer, nullable=True),     # empty = every station that has the signal
              Column("active", Boolean, nullable=False, default=True))

rule_state = Table("andon_rule_state", metadata,
                   Column("rule_id", Integer, primary_key=True),
                   Column("station_id", Integer, primary_key=True),
                   Column("since", DateTime, nullable=False),
                   Column("fired", Boolean, nullable=False, default=False))

notify_channels = Table("andon_notify_channels", metadata,
                        Column("id", Integer, primary_key=True, autoincrement=True),
                        Column("name", String(80), nullable=False),
                        Column("kind", String(12), nullable=False, default="log"),     # log | teams | webhook | sms
                        Column("target", String(500), nullable=True),                  # URL, phone numbers, or ${SECRET_NAME}
                        Column("trigger", String(12), nullable=False, default="red"),  # red | escalation | all
                        Column("roles", String(300), nullable=True),                   # JSON list of escalation roles; empty = any
                        Column("lines", String(200), nullable=True),                   # JSON list of line ids; empty = all
                        Column("active", Boolean, nullable=False, default=True))

notify_log = Table("andon_notify_log", metadata,
                   Column("id", Integer, primary_key=True, autoincrement=True),
                   Column("ts", DateTime, nullable=False),
                   Column("channel_id", Integer, nullable=False),
                   Column("call_id", Integer, nullable=False),
                   Column("event", String(12), nullable=False),          # new | red | L2 | L3 | test
                   Column("status", String(12), nullable=False),         # sending | sent | logged | failed | throttled
                   Column("message", String(500), nullable=True),
                   Column("detail", String(300), nullable=True),
                   UniqueConstraint("channel_id", "call_id", "event", name="uq_notify_once"))

audit_log = Table("andon_audit", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("ts", DateTime, nullable=False, index=True),
                  Column("actor", String(80), nullable=False),
                  Column("action", String(40), nullable=False, index=True),
                  Column("details", Text, nullable=True),
                  Column("client", String(64), nullable=True))   # IP address or device, when known

# ---- event catalogue: every event from any source, with the short name operators see ----------------
events = Table("andon_events", metadata,
               Column("id", Integer, primary_key=True, autoincrement=True),
               Column("source", String(20), nullable=False),     # machine | rule | operator | mes | scada | kafka ...
               Column("code", String(60), nullable=False),       # E-204, FIL-12, rule:3, call:1 ...
               Column("raw_text", String(200), nullable=True),   # original text from the source, never shown to operators
               Column("short_name", String(40), nullable=False),     # what the board shows (English)
               Column("short_name_da", String(40), nullable=True),   # Danish
               Column("call_type_id", Integer, nullable=True),
               Column("line_stop", Boolean, nullable=False, default=False),   # red (stop) or yellow (issue)
               Column("priority", Integer, nullable=False, default=2),        # 1 high, 2 normal, 3 low
               Column("show_on_board", Boolean, nullable=False, default=True),
               Column("oee_category", String(20), nullable=False, default="unplanned"),  # unplanned | planned | external
               Column("renamed", Boolean, nullable=False, default=False),     # False = still the app's default name
               Column("renamed_by", String(80), nullable=True),
               Column("renamed_at", DateTime, nullable=True),
               Column("first_seen", DateTime, nullable=True),
               Column("active", Boolean, nullable=False, default=True))

event_log = Table("andon_event_log", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("event_id", Integer, nullable=False, index=True),
                  Column("line_id", Integer, nullable=False),
                  Column("station_id", Integer, nullable=False, index=True),
                  Column("started_at", DateTime, nullable=False, index=True),
                  Column("ended_at", DateTime, nullable=True),
                  Column("call_id", Integer, nullable=True))

boards = Table("andon_boards", metadata,
               Column("id", Integer, primary_key=True, autoincrement=True),
               Column("name", String(80), nullable=False),
               Column("kind", String(10), nullable=False, default="overview"),  # line (one line, shop-floor screen) | overview
               Column("theme", String(8), nullable=False, default="light"),     # light (default, colour-blind friendly) | dark
               Column("lines", String(200), nullable=True),          # JSON list of line ids; empty = all
               Column("tile_label", String(12), nullable=False, default="event"),  # event | call_type | both
               Column("language", String(4), nullable=False, default="en"),       # en | da
               Column("show_process", Boolean, nullable=False, default=True),
               Column("show_speed", Boolean, nullable=False, default=True),
               Column("show_responder", Boolean, nullable=False, default=True),
               Column("show_ticker", Boolean, nullable=False, default=True),
               Column("show_recent", Boolean, nullable=False, default=True),      # recently fixed, with duration
               Column("show_kpis", Boolean, nullable=False, default=True),
               Column("sound", Boolean, nullable=False, default=True),       # chime on new calls, stops, escalations
               Column("sort", Integer, nullable=False, default=0),
               Column("active", Boolean, nullable=False, default=True))

batches = Table("andon_batches", metadata,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("line_id", Integer, nullable=False, index=True),
                Column("product", String(120), nullable=False),
                Column("batch_no", String(60), nullable=False),
                Column("destination", String(80), nullable=True),
                Column("planned_qty", Float, nullable=True),
                Column("started_at", DateTime, nullable=False),
                Column("ended_at", DateTime, nullable=True),
                Column("source", String(12), nullable=False, default="manual"),   # manual | connector
                Column("started_by", String(80), nullable=True))

oee_hourly = Table("andon_oee_hourly", metadata,
                   Column("station_id", Integer, primary_key=True),
                   Column("hour", DateTime, primary_key=True),       # UTC hour start
                   Column("line_id", Integer, nullable=False, index=True),
                   Column("calendar_s", Float, nullable=False, default=0),
                   Column("not_scheduled_s", Float, nullable=False, default=0),
                   Column("planned_s", Float, nullable=False, default=0),
                   Column("external_s", Float, nullable=False, default=0),
                   Column("unplanned_s", Float, nullable=False, default=0),
                   Column("micro_s", Float, nullable=False, default=0),
                   Column("nodata_s", Float, nullable=False, default=0),
                   Column("run_s", Float, nullable=False, default=0),
                   Column("good", Float, nullable=False, default=0),
                   Column("scrap", Float, nullable=False, default=0),
                   Column("rate", Float, nullable=True))              # target units per minute

oee_targets = Table("andon_oee_targets", metadata,
                    Column("id", Integer, primary_key=True, autoincrement=True),
                    Column("line_id", Integer, nullable=True),        # empty = plant default
                    Column("metric", String(20), nullable=False),     # oee, tcu, oee1, oee2, oee3, availability, performance, quality
                    Column("target", Float, nullable=False))

handover = Table("andon_handover", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("line_id", Integer, nullable=False, index=True),
                 Column("ts", DateTime, nullable=False),
                 Column("shift", String(20), nullable=True),
                 Column("author", String(80), nullable=False),
                 Column("note", Text, nullable=False))

settings = Table("andon_settings", metadata,
                 Column("key", String(60), primary_key=True),
                 Column("value", Text, nullable=False))

ROLES = ["Team leader", "Maintenance", "Quality", "Logistics", "Supervisor", "Production manager"]
# Access levels, lowest to highest. "display" is a TV screen (device key, no sign-in).
LEVELS = {
    "operator": "Operator",
    "responder": "Responder (maintenance, quality, logistics)",
    "supervisor": "Supervisor",
    "line_manager": "Line manager",
    "viewer": "Viewer (management, QA, read-only)",
    "engineer": "Process engineer / BA / IT",
    "admin": "Admin",
}
PROCESSES = ["Production", "Mixing", "Filling", "Assembly", "Inspection", "Labelling", "Packaging", "Palletising", "Utilities"]
OEE_CATEGORIES = {"unplanned": "Unplanned stop (counts against the line)", "planned": "Planned stop (changeover, cleaning, planned maintenance)",
                  "external": "External stop (no material, no staff, utilities)"}
DEFAULT_SETTINGS = {
    "plant_name": "Demo Plant",
    "timezone": "Europe/Copenhagen",
    "shifts": json.dumps([["Early", "06:00", "14:00"], ["Late", "14:00", "22:00"], ["Night", "22:00", "06:00"]]),
    "demo_mode": "1",
    "cancel_window_min": "2",
    "simulator": "1",            # generate machine signals inside the app (demo / no connector)
    "sim_last": "1970-01-01T00:00:00",
    "history_days": "3",
    "history_every_s": "60",     # numeric values are kept in history at most this often (states on every change)
    "microstop_s": "120",        # stops shorter than this count as microstops (performance loss), not as stops
    "oee_headline_base": "oee2",  # which time base the headline OEE (A x P x Q) uses: oee2 (planned production time) or oee1
    "rollup_last": "1970-01-01T00:00:00",
    "notify_last": "1970-01-01T00:00:00",
    "app_url": "",               # public address of the app, used for links in notifications
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def hash_pin(pin: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", str(pin).encode(), salt.encode(), 60_000).hex()
    return f"{salt}${digest}"


def check_pin(pin: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return secrets.compare_digest(hash_pin(pin, salt), stored)


def make_engine(url: str | None) -> Engine:
    """Create an engine from a database URL. Accepts the postgres:// form that many hosts hand out."""
    if not url:
        url = "sqlite:///andon.db"
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False}, pool_pre_ping=True)
    # Encrypt connections to any database that is not on this machine, unless the URL says otherwise.
    from sqlalchemy.engine import make_url
    u = make_url(url)
    args = {}
    if "sslmode" not in u.query and u.host not in (None, "", "localhost", "127.0.0.1", "::1") and not str(u.host).startswith("/"):
        args["sslmode"] = "require"
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, pool_recycle=300, connect_args=args)


def ensure_columns(engine: Engine) -> list[str]:
    """Add columns that newer versions introduced to tables created by older versions (simple, additive only)."""
    from sqlalchemy import inspect, text
    added = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    with engine.begin() as cx:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = ""
                if col.default is not None and getattr(col.default, "arg", None) is not None and not callable(col.default.arg):
                    v = col.default.arg
                    default = f" DEFAULT {(('TRUE' if v else 'FALSE') if engine.dialect.name != 'sqlite' else int(v)) if isinstance(v, bool) else repr(v) if isinstance(v, str) else v}"
                cx.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {col.name} {ddl_type}{default}'))
                added.append(f"{table.name}.{col.name}")
    return added


def init_db(engine: Engine, seed_demo: bool = True) -> bool:
    """Create tables if needed and seed a demo configuration on first run. Returns True if it seeded."""
    metadata.create_all(engine)
    try:
        ensure_columns(engine)
    except Exception:  # a read-only account cannot alter tables; the owner runs upgrades
        pass
    with engine.begin() as cx:
        existing = bool(cx.execute(select(func.count()).select_from(lines)).scalar())
        if not existing:
            for k, v in DEFAULT_SETTINGS.items():
                cx.execute(insert(settings).values(key=k, value=v))
    if existing:
        try:  # an existing plant upgraded from an earlier version (the connector's account may not be allowed: fine)
            with engine.begin() as cx:
                upgrade_defaults(cx)
        except Exception:
            pass
        return False
    if seed_demo:
        from .demo import seed_all
        seed_all(engine)
    return True


PLANNED_DEFAULTS = [("OP-01", "Changeover"), ("OP-02", "Cleaning"), ("OP-03", "Planned maintenance"), ("OP-04", "Break / meeting")]


def upgrade_defaults(cx) -> None:
    """Things later versions need that an older database does not have yet: one board and planned-stop reasons."""
    if not cx.execute(select(func.count()).select_from(boards)).scalar():
        cx.execute(insert(boards).values(name="Management overview", kind="overview", lines=None, tile_label="event",
                                         language="en", sort=0, active=True))
    # boards made before board types existed: a board for exactly one line becomes that line's screen
    for b in cx.execute(select(boards.c.id, boards.c.lines, boards.c.kind)).all():
        try:
            ids = json.loads(b.lines) if b.lines else []
        except ValueError:
            ids = []
        if len(ids) == 1 and b.kind != "line" and not cx.execute(select(func.count()).select_from(audit_log).where(
                audit_log.c.action == "board_type_set", audit_log.c.details == f"board {b.id}")).scalar():
            cx.execute(update(boards).where(boards.c.id == b.id).values(kind="line"))
            audit(cx, "upgrade", "board_type_set", f"board {b.id}")
    if not cx.execute(select(func.count()).select_from(events).where(events.c.code.like("OP-%"))).scalar():
        from . import events as ev
        for code, name in PLANNED_DEFAULTS:
            ev.resolve(cx, "machine", f"{code} {name}", code=code,
                       defaults=dict(short_name=name, line_stop=True, oee_category="planned", show_on_board=False))


DEMO_PINS = ("0000", "1111", "1234", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "4321")


def secure_demo(engine: Engine, admin_pin: str | None) -> dict:
    """Private demo: the PINs published for the public demo must not open anything.
    Demo accounts with a published PIN are disabled (their history stays), the demo access request is withdrawn, and
    the admin account gets the PIN from the secrets (ADMIN_PIN). Runs at every start, so changing ADMIN_PIN works."""
    import secrets as _s
    out = {"disabled": 0, "admin": False}
    admin_pin = str(admin_pin).strip() if admin_pin else ""
    if admin_pin and (admin_pin in DEMO_PINS or not admin_pin.isdigit() or len(admin_pin) < 8):
        admin_pin = ""  # a weak or published admin PIN is ignored: the admin account is disabled instead
    with engine.begin() as cx:
        users = cx.execute(select(responders)).mappings().all()
        admin_ok = bool(admin_pin) and any(check_pin(admin_pin, u["pin_hash"]) and (u["is_admin"] or u["level"] == "admin")
                                           and u["status"] == "active" for u in users)
        for u in users:
            if u["status"] not in ("active", "pending"):
                continue
            demo = any(check_pin(p, u["pin_hash"]) for p in DEMO_PINS)
            if not demo:
                continue
            is_admin = u["is_admin"] or u["level"] == "admin"
            if is_admin and admin_pin and not admin_ok:
                cx.execute(update(responders).where(responders.c.id == u["id"]).values(pin_hash=hash_pin(admin_pin)))
                audit(cx, "setup", "admin_pin_set", f"user {u['id']} · from ADMIN_PIN secret")
                admin_ok = out["admin"] = True
                continue
            cx.execute(update(responders).where(responders.c.id == u["id"]).values(
                pin_hash=hash_pin(_s.token_hex(16)), status="rejected" if u["status"] == "pending" else "disabled"))
            out["disabled"] += 1
        if out["disabled"]:
            audit(cx, "setup", "demo_pins_disabled", f"{out['disabled']} demo account(s) with published PINs")
    return out


def recent_requests(engine: Engine, client: str | None, minutes: int = 60) -> int:
    q = select(func.count()).select_from(audit_log).where(audit_log.c.action == "access_requested",
                                                          audit_log.c.ts >= utcnow() - timedelta(minutes=minutes))
    if client:
        q = q.where(audit_log.c.client == client)
    with engine.connect() as cx:
        return int(cx.execute(q).scalar() or 0)


# ------------------------------------------------------------------ reads
def read_df(engine: Engine, table: Table, active_only: bool = False) -> pd.DataFrame:
    q = select(table)
    if active_only and "active" in table.c:
        q = q.where(table.c.active.is_(True))
    if "sort" in table.c:
        q = q.order_by(table.c.sort, table.c.id)
    elif "id" in table.c:
        q = q.order_by(table.c.id)
    with engine.connect() as cx:
        return pd.read_sql(q, cx)


def get_settings(engine: Engine) -> dict:
    with engine.connect() as cx:
        rows = cx.execute(select(settings)).all()
    out = dict(DEFAULT_SETTINGS)
    out.update({r.key: r.value for r in rows})
    return out


def set_setting(engine: Engine, key: str, value: str) -> None:
    with engine.begin() as cx:
        n = cx.execute(update(settings).where(settings.c.key == key).values(value=value)).rowcount
        if not n:
            cx.execute(insert(settings).values(key=key, value=value))


def open_calls(engine: Engine) -> pd.DataFrame:
    q = select(calls).where(calls.c.status.in_(["open", "ack"])).order_by(calls.c.raised_at)
    with engine.connect() as cx:
        return pd.read_sql(q, cx, parse_dates=["raised_at", "stopped_at", "ack_at", "resolved_at"])


def calls_between(engine: Engine, start: datetime, end: datetime) -> pd.DataFrame:
    q = select(calls).where(calls.c.raised_at >= start, calls.c.raised_at < end, calls.c.status != "cancelled")
    with engine.connect() as cx:
        return pd.read_sql(q, cx, parse_dates=["raised_at", "stopped_at", "ack_at", "resolved_at"])


def audit(target, actor: str, action: str, details: str = "", client: str | None = None) -> None:
    """Append to the audit log. `target` is an Engine or an open connection (to join its transaction)."""
    row = dict(ts=utcnow(), actor=(actor or "unknown")[:80], action=action[:40], details=(details or "")[:2000],
               client=(client or None) and str(client)[:64])
    if hasattr(target, "begin") and not hasattr(target, "in_transaction"):
        with target.begin() as cx:
            cx.execute(insert(audit_log).values(**row))
    else:
        target.execute(insert(audit_log).values(**row))


def failed_logins(engine: Engine, client: str | None, minutes: int = 10) -> int:
    q = select(func.count()).select_from(audit_log).where(audit_log.c.action == "pin_failed",
                                                          audit_log.c.ts >= utcnow() - timedelta(minutes=minutes))
    if client:
        q = q.where(audit_log.c.client == client)
    with engine.connect() as cx:
        return int(cx.execute(q).scalar() or 0)


def verify_pin(engine: Engine, pin: str, admin: bool = False):
    """Return the active user matching the PIN, or None."""
    if not pin:
        return None
    q = select(responders).where(responders.c.active.is_(True), responders.c.status == "active")
    with engine.connect() as cx:
        for r in cx.execute(q).mappings():
            if check_pin(pin, r["pin_hash"]) and (not admin or r["is_admin"] or r["level"] == "admin"):
                return dict(r)
    return None


def pin_in_use(engine: Engine, pin: str, exclude_id: int | None = None) -> bool:
    q = select(responders.c.id, responders.c.pin_hash).where(responders.c.status.in_(["active", "pending"]))
    with engine.connect() as cx:
        return any(check_pin(pin, r.pin_hash) and r.id != exclude_id for r in cx.execute(q))


def request_access(engine: Engine, name: str, email: str, level: str, lines: list[int] | None, note: str, pin: str,
                   client: str | None = None) -> int:
    clash = pin_in_use(engine, pin)
    with engine.begin() as cx:
        rid = cx.execute(insert(responders).values(pin_clash=clash, 
            name=name.strip()[:80], email=(email or "").strip()[:120] or None, role="Team leader", pin_hash=hash_pin(pin),
            is_admin=False, active=True, level=level, status="pending", lines=json.dumps(lines) if lines else None,
            request_note=(note or "").strip()[:1000], requested_at=utcnow())).inserted_primary_key[0]
        audit(cx, name, "access_requested", f"user {rid} · level {level}", client)
    return rid


def decide_access(engine: Engine, user_id: int, approve: bool, decided_by: str, level: str | None = None,
                  role: str | None = None, client: str | None = None) -> None:
    values = dict(status="active" if approve else "rejected", decided_by=decided_by, decided_at=utcnow())
    if level:
        values["level"] = level
        values["is_admin"] = level == "admin"
    if role:
        values["role"] = role
    with engine.begin() as cx:
        cx.execute(update(responders).where(responders.c.id == user_id).values(**values))
        audit(cx, decided_by, "access_approved" if approve else "access_rejected",
              f"user {user_id}{' · level ' + level if level else ''}", client)


# ------------------------------------------------------------------ call lifecycle
def raise_call(engine: Engine, line_id: int, station_id: int, call_type_id: int, stopped: bool, note: str = "",
               source: str = "manual", rule_id: int | None = None, cx=None, actor: str | None = None,
               client: str | None = None, event_id: int | None = None) -> int | None:
    """Create a call unless the same type is already open at this station. Returns the id or None."""
    now = utcnow()

    def _do(c):
        dup = c.execute(select(calls.c.id, calls.c.stopped_at).where(
            calls.c.station_id == station_id, calls.c.call_type_id == call_type_id, calls.c.status.in_(["open", "ack"]))).first()
        if dup:
            if stopped and dup.stopped_at is None:  # an existing call now stops the line
                c.execute(update(calls).where(calls.c.id == dup.id).values(stopped_at=now))
            return None
        ev_id = event_id
        if ev_id is None and source == "manual":  # no specific problem picked: the call type is the event
            from . import events
            tname = c.execute(select(call_types.c.name).where(call_types.c.id == call_type_id)).scalar() or "Call"
            ev_id = events.resolve(c, "operator", tname, code=f"call:{call_type_id}",
                                   defaults=dict(short_name=tname[:40], call_type_id=call_type_id, line_stop=False,
                                                 oee_category="unplanned"))["id"]
        res = c.execute(insert(calls).values(line_id=line_id, station_id=station_id, call_type_id=call_type_id,
                                             status="open", raised_at=now, stopped_at=now if stopped else None,
                                             note=note.strip() or None, source=source, rule_id=rule_id, event_id=ev_id))
        cid = res.inserted_primary_key[0]
        if source == "manual":
            from . import events
            events.open_occurrence(c, ev_id, line_id, station_id, now, call_id=cid)
        audit(c, f"rule {rule_id}" if source == "auto" else (actor or f"station {station_id}"), "call_raised",
              f"call {cid} · station {station_id} · type {call_type_id}{' · line stopped' if stopped else ''}", client)
        return cid

    if cx is not None:
        return _do(cx)
    with engine.begin() as c:
        return _do(c)


def mark_stopped(engine: Engine, call_id: int, client: str | None = None) -> None:
    with engine.begin() as cx:
        n = cx.execute(update(calls).where(calls.c.id == call_id, calls.c.stopped_at.is_(None),
                                           calls.c.status.in_(["open", "ack"])).values(stopped_at=utcnow())).rowcount
        if n:
            audit(cx, "station tablet", "line_stopped", f"call {call_id}", client)


def acknowledge(engine: Engine, call_id: int, who: str, client: str | None = None) -> bool:
    with engine.begin() as cx:
        n = cx.execute(update(calls).where(calls.c.id == call_id, calls.c.status == "open")
                       .values(status="ack", ack_at=utcnow(), ack_by=who)).rowcount
        if n:
            audit(cx, who, "call_acknowledged", f"call {call_id}", client)
    return bool(n)


def resolve(engine: Engine, call_id: int, who: str, reason_id: int | None, note: str = "", client: str | None = None) -> bool:
    now = utcnow()
    with engine.begin() as cx:
        row = cx.execute(select(calls.c.ack_at, calls.c.ack_by).where(calls.c.id == call_id)).first()
        if row is None:
            return False
        values = dict(status="resolved", resolved_at=now, resolved_by=who, reason_id=reason_id,
                      resolution_note=note.strip() or None)
        if row.ack_at is None:  # resolved without a separate acknowledgement
            values.update(ack_at=now, ack_by=who)
        n = cx.execute(update(calls).where(calls.c.id == call_id, calls.c.status.in_(["open", "ack"])).values(**values)).rowcount
        if n:
            from . import events
            st_id = cx.execute(select(calls.c.station_id).where(calls.c.id == call_id)).scalar()
            events.close_occurrences(cx, st_id, now, call_id=call_id)
            audit(cx, who, "call_resolved", f"call {call_id} · reason {reason_id}", client)
    return bool(n)


def cancel(engine: Engine, call_id: int, window_min: int, client: str | None = None) -> bool:
    """Operators may cancel a call raised by mistake, within a short window and before anyone acknowledges it."""
    with engine.begin() as cx:
        row = cx.execute(select(calls.c.raised_at, calls.c.status).where(calls.c.id == call_id)).first()
        if not row or row.status != "open" or (utcnow() - row.raised_at).total_seconds() > window_min * 60:
            return False
        cx.execute(update(calls).where(calls.c.id == call_id).values(status="cancelled", resolved_at=utcnow()))
        cx.execute(event_log.delete().where(event_log.c.call_id == call_id))  # a mistake is not an event
        audit(cx, "station tablet", "call_cancelled", f"call {call_id}", client)
    return True


def delete_all_calls(engine: Engine, actor: str = "admin", client: str | None = None) -> None:
    with engine.begin() as cx:
        cx.execute(calls.delete())
        audit(cx, actor, "calls_deleted", "all calls deleted", client)


# ------------------------------------------------------------------ admin saves
def save_table(engine: Engine, table: Table, edited: pd.DataFrame, original: pd.DataFrame, columns: list[str],
               actor: str = "admin", client: str | None = None) -> tuple[int, int, int]:
    """Apply a data-editor result: update changed rows, insert new rows, deactivate removed rows."""
    added = updated = removed = 0
    orig = {int(r["id"]): r for _, r in original.iterrows()}
    seen = set()

    def clean(v):
        if v is None or (isinstance(v, float) and pd.isna(v)) or v is pd.NA:
            return None
        if hasattr(v, "item"):
            return v.item()
        return v

    with engine.begin() as cx:
        for _, r in edited.iterrows():
            values = {c: clean(r.get(c)) for c in columns}
            rid = clean(r.get("id"))
            if rid is not None and int(rid) in orig:
                seen.add(int(rid))
                if any(values[c] != clean(orig[int(rid)].get(c)) for c in columns):
                    cx.execute(update(table).where(table.c.id == int(rid)).values(**values))
                    updated += 1
            elif any(v not in (None, "") for v in values.values()):
                if "active" in table.c and values.get("active") is None:
                    values["active"] = True
                cx.execute(insert(table).values(**values))
                added += 1
        for rid in set(orig) - seen:
            if "active" in table.c:
                cx.execute(update(table).where(table.c.id == rid).values(active=False))
            removed += 1
        if added or updated or removed:
            audit(cx, actor, "config_changed", f"{table.name}: {added} added, {updated} changed, {removed} removed", client)
    return added, updated, removed
