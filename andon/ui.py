"""Shared Streamlit helpers: database connection, cached configuration, styling."""
from __future__ import annotations

import os

import pandas as pd
import streamlit as st

from . import db, logic


def secret(name: str, default=None):
    try:
        value = st.secrets.get(name)
    except Exception:
        value = None
    return value or os.environ.get(name) or default


def private() -> bool:
    """Private demo (secret PRIVATE_DEMO = "1"): visitors see only a welcome page with a request form until an admin
    approves them. Set in the hosting platform's secrets, never from inside the app."""
    return str(secret("PRIVATE_DEMO", "0")).strip() == "1"


@st.cache_resource(show_spinner="Connecting to the database…")
def engine():
    url = secret("DATABASE_URL")
    e = db.make_engine(url)
    db.init_db(e, seed_demo=str(secret("SEED_DEMO", "1")) != "0")
    if private():
        db.secure_demo(e, secret("ADMIN_PIN"))  # the public demo PINs stop working; the admin PIN comes from the secrets
    return e


def device_ok() -> bool:
    """A tablet or TV opened with the right ?key=DEVICE_KEY."""
    import hmac
    key = secret("DEVICE_KEY")
    given = st.query_params.get("key", "")
    return bool(key and given and hmac.compare_digest(str(given), str(key)))


def using_sqlite() -> bool:
    return engine().dialect.name == "sqlite"


@st.cache_data(ttl=60, show_spinner=False)
def _cfg(_v: int) -> dict:
    e = engine()
    out = {}
    for key, table in [("lines", db.lines), ("stations", db.stations), ("call_types", db.call_types),
                       ("reasons", db.reasons), ("responders", db.responders), ("events", db.events), ("boards", db.boards)]:
        df = db.read_df(e, table)
        out[f"{key}_all"] = df
        out[key] = df[df["active"].fillna(False).astype(bool)].reset_index(drop=True) if "active" in df else df
    out["responders"] = out["responders"].drop(columns=["pin_hash"])
    out["responders_all"] = out["responders_all"].drop(columns=["pin_hash"])
    # only stations on active lines
    out["stations"] = out["stations"][out["stations"]["line_id"].isin(out["lines"]["id"])].reset_index(drop=True)
    out["settings"] = db.get_settings(e)
    return out


def cfg() -> dict:
    return _cfg(st.session_state.get("cfg_v", 0))


def refresh_cfg() -> None:
    _cfg.clear()
    st.session_state["cfg_v"] = st.session_state.get("cfg_v", 0) + 1


def zone():
    return logic.tz(cfg()["settings"])


def shifts():
    return logic.parse_shifts(cfg()["settings"])


def now_local():
    return pd.Timestamp(db.utcnow()).tz_localize(logic.UTC).tz_convert(zone()).to_pydatetime()


def station_label(c: dict, station_id: int) -> str:
    s = c["stations_all"].set_index("id")
    l = c["lines_all"].set_index("id")
    if station_id not in s.index:
        return "Unknown station"
    return f"{l.loc[s.loc[station_id, 'line_id'], 'name']} · {s.loc[station_id, 'name']}"


BASE_CSS = """
<style>
  .block-container {padding-top: 1.6rem; padding-bottom: 2rem;}
  [data-testid="stMetricValue"] {font-variant-numeric: tabular-nums;}
  .an-chip {display:inline-flex; align-items:center; gap:.35rem; padding:.18rem .6rem; border-radius:999px;
            font-size:.78rem; font-weight:600; letter-spacing:.02em}
  .an-green {background:#dcfce7; color:#166534}
  .an-yellow {background:#fef3c7; color:#92400e}
  .an-red {background:#fee2e2; color:#991b1b}
  .an-blue {background:#dbeafe; color:#1e40af}
  .an-muted {opacity:.7; font-size:.85rem}
</style>
"""


def css(extra: str = "") -> None:
    st.markdown(BASE_CSS + extra, unsafe_allow_html=True)


def demo_pin_hint() -> None:
    if cfg()["settings"].get("demo_mode") == "1" and not private():
        st.caption("Demo PINs: Team leader 1111 · Maintenance 2222 · Quality 3333 · Logistics 4444 · "
                   "Supervisor 5555 · Production manager 6666 · Admin 0000")


# ------------------------------------------------------------------ access control
MAX_FAILED = 5          # wrong PINs allowed per device in LOCK_MIN minutes
GLOBAL_MAX_FAILED = 40  # private demo: wrong PINs from all devices together before sign-in pauses
LOCK_MIN = 10


def client_id() -> str:
    """The viewer's IP address when Streamlit knows it, otherwise this browser session."""
    ip = None
    try:
        ip = st.context.ip_address
    except Exception:
        pass
    if not isinstance(ip, str) or not ip:
        ip = "session-" + st.session_state.setdefault("_sid", __import__("secrets").token_hex(4))
    return str(ip)


def sso_configured() -> bool:
    try:
        return "auth" in st.secrets and hasattr(st, "login")
    except Exception:
        return False


def sso_user():
    try:
        if sso_configured() and st.user.is_logged_in:
            return st.user
    except Exception:
        pass
    return None


def _domain_ok(email: str) -> bool:
    allowed = [d.strip().lower().lstrip("@") for d in str(secret("AUTH_ALLOWED_DOMAINS", "")).split(",") if d.strip()]
    return not allowed or (email or "").lower().rsplit("@", 1)[-1] in allowed


def require(kind: str) -> None:
    """Gatekeeper at the top of every page.
    floor  (operator tablets, TV board): needs ?key=DEVICE_KEY when a device key is set, or a signed-in user.
    staff  (responder, machines, analytics, admin): needs company sign-in when single sign-on is configured."""
    import hmac
    user = sso_user()
    if user is not None and not _domain_ok(getattr(user, "email", "")):
        st.error(f"{user.email} is not allowed to use this app.")
        st.button("Sign out", on_click=st.logout)
        st.stop()
    if kind == "floor":
        key = secret("DEVICE_KEY")
        if private():  # private demo: shop-floor pages need an approved sign-in or a device key
            if current_user() is not None or device_ok():
                return
            st.info("This is a private demo. Request access on the welcome page.", icon=":material/lock:")
            st.stop()
        if not key or user is not None:
            return
        given = st.query_params.get("key", "")
        if given and hmac.compare_digest(str(given), str(key)):
            return
        st.error("This screen needs a device key. Open it with the link from Admin → Security "
                 "(it ends with ?key=…), or sign in.", icon=":material/lock:")
        if sso_configured():
            st.button("Sign in with your company account", on_click=st.login, type="primary")
        st.stop()
    if kind == "staff" and sso_configured() and user is None:
        st.markdown("## Sign in")
        st.write("This page is for plant staff. Sign in with your company account.")
        st.button("Sign in", on_click=st.login, type="primary")
        st.stop()


def check_pin(pin: str, admin: bool = False):
    """Verify a PIN with lock-out after repeated failures. Returns (responder | None, message | None)."""
    e = engine()
    cl = client_id()
    if db.failed_logins(e, cl, LOCK_MIN) >= MAX_FAILED:
        return None, f"Too many wrong PINs from this device. Try again in {LOCK_MIN} minutes."
    if private() and db.failed_logins(e, None, LOCK_MIN) >= GLOBAL_MAX_FAILED:
        # someone is guessing from many devices: pause all PIN sign-ins for a while
        return None, f"Sign-in is paused for a few minutes because of many wrong PINs. Try again in {LOCK_MIN} minutes."
    r = db.verify_pin(e, pin, admin=admin)
    if r is None:
        db.audit(e, "unknown", "pin_failed", "admin sign-in" if admin else "responder PIN", cl)
        left = MAX_FAILED - db.failed_logins(e, cl, LOCK_MIN)
        return None, "That PIN is not recognised." + (f" {left} attempt{'s' if left != 1 else ''} left." if left <= 2 else "")
    db.audit(e, r["name"], "admin_signed_in" if admin else "signed_in", "", cl)
    return r, None


# ------------------------------------------------------------------ users, levels and permissions
SESSION_HOURS = 8
IDLE_MIN = 30  # admins and engineers
PAGES: dict = {}  # filled by app.py so pages can link to each other
PERMS = {
    "raise": {"operator", "responder", "supervisor", "line_manager", "engineer", "admin"},
    "respond": {"responder", "supervisor", "line_manager", "engineer", "admin"},
    "machines": {"responder", "supervisor", "line_manager", "viewer", "engineer", "admin"},
    "analytics": {"supervisor", "line_manager", "viewer", "engineer", "admin"},
    "batch": {"supervisor", "line_manager", "engineer", "admin"},
    "handover": {"supervisor", "line_manager", "engineer", "admin"},
    "planned_stop": {"operator", "supervisor", "line_manager", "engineer", "admin"},
    "oee_supervisor": {"supervisor"},
    "oee_full": {"line_manager", "viewer", "engineer", "admin"},
    "overview": {"line_manager", "viewer", "engineer", "admin"},   # management overview boards (several lines)
    "configure": {"engineer", "admin"},
    "admin": {"admin"},
}


def current_user() -> dict | None:
    from datetime import datetime, timedelta
    u = st.session_state.get("user")
    now = datetime.now()
    if u and u.get("until", datetime.min) > now:
        # people who can change configuration are signed out after 30 minutes without activity
        if u.get("level") in ("admin", "engineer") and u.get("via") == "pin":
            if now - u.get("seen", now) > timedelta(minutes=IDLE_MIN):
                st.session_state.pop("user", None)
                return None
            u["seen"] = now
        return u
    st.session_state.pop("user", None)
    # company sign-in: map the signed-in email to an approved user
    su = sso_user()
    if su is not None and getattr(su, "email", None):
        from sqlalchemy import func, select
        with engine().connect() as cx:
            r = cx.execute(select(db.responders).where(func.lower(db.responders.c.email) == su.email.lower(),
                                                       db.responders.c.status == "active",
                                                       db.responders.c.active.is_(True))).mappings().first()
        if r:
            return _start_session(dict(r), via="sso")
    return None


def _start_session(r: dict, via: str = "pin") -> dict:
    import json
    from datetime import datetime, timedelta
    lines = None
    if r.get("lines"):
        try:
            lines = json.loads(r["lines"])
        except (ValueError, TypeError):
            lines = None
    u = {"id": int(r["id"]), "name": r["name"], "level": r.get("level") or "responder", "role": r.get("role"),
         "lines": lines, "via": via, "until": datetime.now() + timedelta(hours=SESSION_HOURS)}
    st.session_state["user"] = u
    return u


def sign_in(pin: str):
    r, msg = check_pin(pin)
    if r:
        return _start_session(r), None
    return None, msg


def sign_out() -> None:
    st.session_state.pop("user", None)
    st.session_state.pop("admin_ok", None)


def level() -> str:
    u = current_user()
    return u["level"] if u else "public"


def can(perm: str) -> bool:
    return level() in PERMS.get(perm, set())


def oee_view() -> str:
    if can("oee_full"):
        return "full"
    if can("oee_supervisor"):
        return "supervisor"
    return "operator"


def allowed_lines(c: dict) -> list[int]:
    u = current_user()
    ids = list(c["lines"]["id"].astype(int))
    if u and u.get("lines"):
        return [i for i in ids if i in set(u["lines"])]
    return ids


def need(perm: str, what: str) -> None:
    """Stop the page with a clear message when the viewer lacks a permission."""
    if can(perm):
        return
    u = current_user()
    st.markdown(f"## {what}")
    if u:
        st.warning(f"Your access level ({db.LEVELS.get(u['level'], u['level'])}) does not include this page. "
                   "Ask for more access on the Account page.", icon=":material/lock:")
    else:
        st.info("Sign in to open this page. If you do not have access yet, request it on the Account page.", icon=":material/lock:")
    if "account" in PAGES:
        st.page_link(PAGES["account"], label="Go to Account", icon=":material/person:")
    st.stop()


LABELS = {
    "en": {"running": "Running", "no_calls": "No open calls", "off": "Not scheduled", "planned": "Planned stop",
           "idle": "Idle", "stop": "STOP", "fixed": "Fixed in", "no_data": "No data", "open_calls": "Open calls",
           "stopped_now": "Stations stopped", "downtime": "Downtime this shift", "response": "Avg response",
           "calls_shift": "Calls this shift", "oee_shift": "OEE this shift", "recent": "Recently fixed", "ack": "Ack",
           "shift": "shift", "waiting": "Waiting for upstream", "line_stopped": "Line stopped", "line_ok": "Running, no open calls",
           "escalated": "Escalated", "s_stop": "Line stopped", "s_problem": "Problem", "s_fault": "Machine stopped",
           "s_planned": "Planned stop", "s_idle": "Waiting", "s_off": "Not scheduled", "s_nodata": "No data", "s_running": "Running"},
    "da": {"running": "Kører", "no_calls": "Ingen åbne kald", "off": "Ikke planlagt", "planned": "Planlagt stop",
           "idle": "Venter", "stop": "STOP", "fixed": "Løst på", "no_data": "Ingen data", "open_calls": "Åbne kald",
           "stopped_now": "Stationer stoppet", "downtime": "Stoptid i skiftet", "response": "Gns. reaktionstid",
           "calls_shift": "Kald i skiftet", "oee_shift": "OEE i skiftet", "recent": "Senest løst", "ack": "Kvitteret",
           "shift": "skift", "waiting": "Venter på forrige station", "line_stopped": "Linje stoppet", "line_ok": "Kører, ingen åbne kald",
           "escalated": "Eskaleret", "s_stop": "Linje stoppet", "s_problem": "Problem", "s_fault": "Maskine stoppet",
           "s_planned": "Planlagt stop", "s_idle": "Venter", "s_off": "Ikke planlagt", "s_nodata": "Ingen data", "s_running": "Kører"},
}
