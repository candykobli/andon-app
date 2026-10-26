"""Admin (admins only): approve access requests, manage users and PINs, security status and audit log,
general settings and demo data."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st
from sqlalchemy import select, update

from andon import db, demo, oee, ui

ss = st.session_state
DEMO_PINS = ("0000", "1111", "1234", "2222", "3333", "4444", "5555", "6666", "7777", "8888")


def _actor() -> str:
    u = ui.current_user()
    return u["name"] if u else "admin"


def render():
    ui.need("admin", "Admin")
    ui.css()
    e = ui.engine()
    c = ui.cfg()
    st.markdown("## Admin")
    pending = db.read_df(e, db.responders)
    pending = pending[pending["status"] == "pending"]
    tabs = st.tabs([f"Access requests ({len(pending)})", "Users & PINs", "Security", "Settings", "Data"])

    # -------------------------------------------------- access requests
    with tabs[0]:
        if pending.empty:
            st.caption("No requests waiting.")
        lmap = c["lines_all"].set_index("id")["name"].to_dict()
        for r in pending.itertuples():
            with st.container(border=True):
                lines = ", ".join(lmap.get(i, str(i)) for i in json.loads(r.lines)) if isinstance(r.lines, str) and r.lines else "all lines"
                st.markdown(f"**{r.name}** · {r.email or 'no email'} · asks for **{db.LEVELS.get(r.level, r.level)}** on {lines}")
                st.caption(f"Requested {pd.Timestamp(r.requested_at).tz_localize('UTC').tz_convert(ui.zone()):%d %b %H:%M}"
                           if pd.notna(r.requested_at) else "")
                if isinstance(r.request_note, str) and r.request_note:
                    st.markdown(f"> {r.request_note}")
                a, b, cc, d = st.columns([2, 2, 1, 1], vertical_alignment="bottom")
                lvl = a.selectbox("Grant level", list(db.LEVELS), index=list(db.LEVELS).index(r.level) if r.level in db.LEVELS else 0,
                                  format_func=db.LEVELS.get, key=f"lvl_{r.id}")
                role = b.selectbox("Escalation role (for responders)", db.ROLES, key=f"role_{r.id}")
                clash = bool(getattr(r, "pin_clash", False))
                if clash:
                    st.warning("This person's PIN is already used by another account. Reject the request and ask them to choose another PIN.")
                if cc.button("Approve", key=f"ok_{r.id}", type="primary", disabled=clash, width="stretch"):
                    db.decide_access(e, int(r.id), True, _actor(), level=lvl, role=role, client=ui.client_id())
                    ui.refresh_cfg()
                    st.toast(f"{r.name} approved as {db.LEVELS[lvl]}")
                    st.rerun()
                if d.button("Reject", key=f"no_{r.id}", width="stretch"):
                    db.decide_access(e, int(r.id), False, _actor(), client=ui.client_id())
                    st.toast(f"Request from {r.name} rejected")
                    st.rerun()
        st.caption("Approvals and rejections are recorded in the audit log with your name.")

    # -------------------------------------------------- users
    with tabs[1]:
        users = db.read_df(e, db.responders, active_only=True)
        users = users[users["status"].isin(["active", "disabled"])].reset_index(drop=True)
        lmap = c["lines_all"].set_index("id")["name"].to_dict()
        lrev = {v: k for k, v in lmap.items()}
        view = users.drop(columns=["pin_hash"]).assign(
            new_pin="", line_names=users["lines"].map(lambda x: ", ".join(lmap.get(i, str(i)) for i in json.loads(x)) if isinstance(x, str) and x else ""))
        ed = st.data_editor(view[["id", "name", "email", "level", "role", "line_names", "status", "new_pin", "is_admin", "active", "lines"]],
                            key="ed_users", num_rows="dynamic", hide_index=True, width="stretch", column_config={
                                "id": None, "active": None, "is_admin": None, "lines": None, "name": "Name", "email": "Email",
                                "level": st.column_config.SelectboxColumn("Access level", options=list(db.LEVELS), required=True),
                                "role": st.column_config.SelectboxColumn("Escalation role", options=db.ROLES, required=True),
                                "line_names": st.column_config.TextColumn("Lines", help="Comma-separated line names; empty = all lines"),
                                "status": st.column_config.SelectboxColumn("Status", options=["active", "disabled"], required=True),
                                "new_pin": st.column_config.TextColumn("New PIN", help="Leave empty to keep the current PIN")})
        st.caption("PINs are stored hashed and cannot be shown. Type a new PIN to change one; new people need a PIN. "
                   "Set status to *disabled* to block someone without losing their history.")
        if st.button("Save users", type="primary"):
            ed = ed.copy()
            new = ed["id"].isna() & ed["name"].fillna("").ne("")
            pins = ed["new_pin"].fillna("").astype(str).str.strip()
            if (new & pins.eq("")).any():
                st.error("Give every new person a PIN.")
            elif (pins.ne("") & ~pins.str.fullmatch(r"\d{4,12}")).any():
                st.error("PINs must be 4–12 digits.")
            elif any(p and _pin_taken(e, p, None if pd.isna(i) else int(i)) for p, i in zip(pins, ed["id"])):
                st.error("One of the new PINs is already used by someone else. Choose another.")
            elif (ed["level"] != "admin").all() and (users["level"] == "admin").any():
                st.error("Keep at least one admin.")
            else:
                orig = users.set_index("id")["pin_hash"]
                ed["pin_hash"] = [db.hash_pin(p) if p else orig.get(i) for p, i in zip(pins, ed["id"])]
                ed["is_admin"] = ed["level"] == "admin"
                ed["active"] = True
                ed["status"] = ed["status"].fillna("active")
                ed["lines"] = ed["line_names"].map(lambda t: json.dumps([lrev[x.strip()] for x in t.split(",") if x.strip() in lrev])
                                                   if isinstance(t, str) and t.strip() else None)
                res = db.save_table(e, db.responders, ed, users, ["name", "email", "level", "role", "lines", "status", "is_admin",
                                                                  "pin_hash", "active"], actor=_actor(), client=ui.client_id())
                ui.refresh_cfg()
                st.toast(f"Saved: {res[0]} added, {res[1]} changed, {res[2]} removed", icon=":material/save:")
                st.rerun()

    with tabs[2]:
        _security_tab(e, c)

    # -------------------------------------------------- settings
    with tabs[3]:
        sett = c["settings"]
        with st.form("settings"):
            plant = st.text_input("Plant name", sett.get("plant_name"))
            tzv = st.text_input("Time zone", sett.get("timezone"), help="IANA name, e.g. Europe/Copenhagen, Europe/Paris, America/New_York")
            c1, c2, c3 = st.columns(3)
            win = c1.number_input("Operators may cancel a call within (min)", 0, 30, int(sett.get("cancel_window_min") or 2))
            hdays = c2.number_input("Keep signal history (days)", 1, 365, int(sett.get("history_days") or 3))
            hevery = c3.number_input("Store numeric values every (s)", 5, 3600, int(sett.get("history_every_s") or 60))
            demo_on = st.toggle("Demo mode", sett.get("demo_mode") == "1",
                                help="Shows demo PINs, a 'Demo bot' closes forgotten calls, and gaps in OEE history are filled with synthetic data.")
            if st.form_submit_button("Save settings", type="primary"):
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(tzv)
                except Exception:
                    st.error("Unknown time zone. Use a name like Europe/Copenhagen.")
                else:
                    db.audit(e, _actor(), "settings_changed", "general settings", ui.client_id())
                    for k, v in {"plant_name": plant, "timezone": tzv, "cancel_window_min": str(win), "history_days": str(hdays),
                                 "history_every_s": str(hevery), "demo_mode": "1" if demo_on else "0"}.items():
                        db.set_setting(e, k, v)
                    ui.refresh_cfg()
                    st.toast("Settings saved", icon=":material/save:")
                    st.rerun()
        st.caption("Boards, units, events, rules, the OEE model and shifts are under **Configure** (process engineers, BAs, IT).")

    # -------------------------------------------------- data
    with tabs[4]:
        st.markdown(f"Database: **{'SQLite file (local)' if ui.using_sqlite() else e.dialect.name.title() + ' (cloud)'}**")
        if ui.using_sqlite():
            st.warning("The app is using a local SQLite file. On Streamlit Community Cloud this resets when the app restarts. "
                       "Add DATABASE_URL in Secrets to use a Postgres database (see README).", icon=":material/database:")
        a, b, cc = st.columns(3)
        if a.button("Add 30 days of demo calls", width="stretch"):
            n = demo.seed_history(e, days=30, seed=int(pd.Timestamp.now().timestamp()) % 10_000)
            st.toast(f"Added {n:,} historical calls")
        if a.button("Rebuild 14 days of demo OEE", width="stretch"):
            n = oee.backfill(e, days=14, seed=int(pd.Timestamp.now().timestamp()) % 10_000)
            st.toast(f"Rebuilt {n:,} hourly OEE rows")
        confirm = b.checkbox("I understand this deletes every call")
        if b.button("Delete all calls", disabled=not confirm, width="stretch"):
            db.delete_all_calls(e, _actor(), ui.client_id())
            st.toast("All calls deleted")
        if cc.button("Delete signal history", width="stretch"):
            with e.begin() as cx:
                cx.execute(db.signal_history.delete())
                db.audit(cx, _actor(), "history_deleted", "signal history", ui.client_id())
            st.toast("Signal history deleted")


def _pin_taken(e, pin, exclude_id) -> bool:
    return db.pin_in_use(e, pin, exclude_id)


def _security_tab(e, c):
    st.markdown("**Security status**")
    checks = []
    default_admin = db.verify_pin(e, "0000", admin=True) is not None
    demo_pins = any(db.verify_pin(e, p) is not None for p in DEMO_PINS[1:])
    checks.append((not default_admin, "Default admin PIN 0000 is disabled",
                   "The default admin PIN 0000 still works. Change it under Responders & PINs."))
    checks.append((not demo_pins, "Demo responder PINs are changed",
                   "Demo PINs still work. Give every user a personal PIN (Users & PINs)."))
    checks.append((c["settings"].get("demo_mode") != "1", "Demo mode is off",
                   "Demo mode is on: demo PINs are shown on screen and a bot closes old calls."))
    checks.append((ui.sso_configured(), "Company sign-in (single sign-on) protects staff pages",
                   "No company sign-in configured: anyone who can reach the app can open the responder, analytics and "
                   "admin pages (actions still need a PIN)."))
    checks.append((bool(ui.secret("DEVICE_KEY")), "Tablets and boards need a device key",
                   "No DEVICE_KEY set: anyone who can reach the app can open the operator and board screens."))
    if ui.using_sqlite():
        checks.append((False, "", "The app is using a local SQLite file. Use a managed PostgreSQL with backups for real use."))
    else:
        checks.append(("sslmode=disable" not in str(e.url), "Database connection is encrypted (TLS)",
                       "The database connection is not encrypted."))
    for ok, good, bad in checks:
        st.markdown(f":green-badge[OK] {good}" if ok else f":orange-badge[Action] {bad}")
    key = ui.secret("DEVICE_KEY")
    if key:
        st.markdown("**Links for floor devices**")
        st.caption("Bookmark these on the tablets and TVs. Anyone with the link can open the screen, so treat it like a "
                   "password. Change DEVICE_KEY in the app secrets to revoke all links at once.")
        st.code(f"Board (TV):  /board?kiosk=1&key={key}\nOperator:    /?station=<id>&key={key}", language=None)
    st.markdown("**Audit log**")
    days = st.selectbox("Show", [1, 7, 30], format_func=lambda d: f"Last {d} day{'s' if d > 1 else ''}", key="audit_days")
    q = (select(db.audit_log).where(db.audit_log.c.ts >= db.utcnow() - timedelta(days=days))
         .order_by(db.audit_log.c.id.desc()).limit(2000))
    with e.connect() as cx:
        log = pd.read_sql(q, cx, parse_dates=["ts"])
    if log.empty:
        st.caption("No events yet.")
        return
    log["ts"] = log["ts"].dt.tz_localize("UTC").dt.tz_convert(ui.zone()).dt.strftime("%d %b %H:%M:%S")
    pick = st.multiselect("Actions", sorted(log["action"].unique()), placeholder="All actions", key="audit_actions")
    if pick:
        log = log[log["action"].isin(pick)]
    fails = int((log["action"] == "pin_failed").sum())
    if fails:
        st.caption(f"{fails} wrong PIN attempt{'s' if fails > 1 else ''} in this period.")
    st.dataframe(log.drop(columns=["id"]), hide_index=True, width="stretch", height=360,
                 column_config={"ts": "Time", "actor": "Who", "action": "Action", "details": "Details", "client": "Device / IP"})
    st.download_button("Download audit log (CSV)", log.to_csv(index=False).encode("utf-8-sig"), "andon_audit.csv", "text/csv")
