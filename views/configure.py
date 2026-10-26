"""Configure (process engineers, BAs, IT developers and admins): boards, lines and units, the event catalogue,
call types, reason codes, signals and rules, the OEE model and targets, and shifts. Operators never see this page."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from andon import db, events, oee, ui

CONDITIONS = {"state_is": "State is (param: e.g. Fault or Idle)", "not_empty": "Has a value (event or fault code present)",
              "outside_limits": "Outside min/max limits", "above_hi": "Above max", "below_lo": "Below min",
              "below_target_pct": "Below % of target (param: %)"}
KINDS = ["state", "rate", "counter", "process"]


def _actor() -> str:
    u = ui.current_user()
    return u["name"] if u else "unknown"


def _saved(res):
    a, u, r = res
    ui.refresh_cfg()
    st.toast(f"Saved: {a} added, {u} changed, {r} removed", icon=":material/save:")
    st.rerun()


def _save(engine, table, edited, original, columns):
    return db.save_table(engine, table, edited, original, columns, actor=_actor(), client=ui.client_id())


def _editor(df, key, cfg, order, dynamic=True):
    return st.data_editor(df[order], key=key, num_rows="dynamic" if dynamic else "fixed", hide_index=True, width="stretch", column_config=cfg)


def render():
    ui.need("configure", "Configure")
    ui.css()
    e = ui.engine()
    c = ui.cfg()
    st.markdown("## Configure")
    st.caption(f"Signed in as {_actor()}. Edit a table, then press its Save button. Removing a row deactivates it, so history "
               "stays intact. Every change is recorded in the audit log.")
    tabs = st.tabs(["Boards", "Lines & units", "Event catalogue", "Call types & escalation", "Reason codes",
                    "Signals & rules", "OEE model & targets", "Shifts & schedule", "Notifications"])
    lines = db.read_df(e, db.lines, active_only=True)
    lmap = dict(zip(lines["id"].astype(int), lines["name"]))
    lrev = {v: k for k, v in lmap.items()}
    tmap = dict(zip(c["call_types"]["id"].astype(int), c["call_types"]["name"]))
    trev = {v: k for k, v in tmap.items()}

    # -------------------------------------------------- boards
    with tabs[0]:
        _boards_tab(e, c, lines, lmap)

    # -------------------------------------------------- lines & units
    with tabs[1]:
        st.markdown("**Lines**")
        ed = _editor(lines, "ed_lines", {"id": None, "active": None, "scheduled_shifts": None, "weekends": None,
                                          "name": "Line", "area": "Area", "sort": st.column_config.NumberColumn("Order", step=1)},
                     ["id", "name", "area", "sort", "active", "scheduled_shifts", "weekends"])
        if st.button("Save lines", type="primary"):
            _saved(_save(e, db.lines, ed.assign(active=ed["active"].fillna(True), area=ed["area"].fillna("Production"),
                                                sort=ed["sort"].fillna(0), weekends=ed["weekends"].fillna(False).astype(bool)),
                         lines, ["name", "area", "sort", "active"]))
        st.markdown("**Units per line** (machines or workstations). Add as many as a line needs.")
        stations = db.read_df(e, db.stations, active_only=True)
        sv = stations.assign(line=stations["line_id"].map(lmap))
        ed = _editor(sv, "ed_stations", {"id": None, "line_id": None, "active": None,
                                         "line": st.column_config.SelectboxColumn("Line", options=list(lmap.values()), required=True),
                                         "name": "Unit", "process": st.column_config.SelectboxColumn("Process", options=db.PROCESSES),
                                         "is_output": st.column_config.CheckboxColumn("Counts line output", help="Used for line OEE. Default: last unit."),
                                         "sort": st.column_config.NumberColumn("Order", step=1)},
                     ["id", "line_id", "line", "name", "process", "is_output", "sort", "active"])
        if st.button("Save units", type="primary"):
            ed = ed.assign(line_id=ed["line"].map(lrev), active=ed["active"].fillna(True), sort=ed["sort"].fillna(0),
                           is_output=ed["is_output"].fillna(False).astype(bool))
            if ed["line_id"].isna().any():
                st.error("Every unit needs a line.")
            else:
                _saved(_save(e, db.stations, ed, stations, ["line_id", "name", "process", "is_output", "sort", "active"]))
        st.caption("After adding a unit, add its machine signals under **Signals & rules** so it shows live data, and map its tags in the connector.")

    # -------------------------------------------------- event catalogue
    with tabs[2]:
        st.markdown("**Event catalogue**: every event from every source (machines, MES/SCADA alarms, rules, operators). "
                    "New events appear automatically with a default name. Give them a short name operators understand; "
                    "that name is what the board shows.")
        ev = db.read_df(e, db.events, active_only=True)
        f1, f2, f3 = st.columns([1.2, 1.2, 2])
        only_new = f1.toggle("Only not yet reviewed", key="ev_new")
        src = f2.multiselect("Source", sorted(ev["source"].unique()), key="ev_src", placeholder="All sources")
        q = f3.text_input("Search", key="ev_q", placeholder="code, original text or name")
        view = ev.copy()
        if only_new:
            view = view[~view["renamed"].astype(bool)]
        if src:
            view = view[view["source"].isin(src)]
        if q.strip():
            ql = q.lower()
            view = view[view[["code", "raw_text", "short_name"]].astype(str).apply(lambda r: ql in " ".join(r).lower(), axis=1)]
        view = view.assign(call_type=view["call_type_id"].map(tmap), name_len=view["short_name"].str.len())
        ed = _editor(view, "ed_events", {
            "id": None, "active": None, "call_type_id": None, "renamed_at": None, "first_seen": None,
            "source": st.column_config.TextColumn("Source", disabled=True),
            "code": st.column_config.TextColumn("Code", disabled=True),
            "raw_text": st.column_config.TextColumn("Original text (engineers only)", disabled=True),
            "short_name": st.column_config.TextColumn("Board name (EN)", max_chars=events.MAX_NAME, required=True),
            "short_name_da": st.column_config.TextColumn("Board name (DA)", max_chars=events.MAX_NAME),
            "name_len": st.column_config.NumberColumn("Length", disabled=True, help=f"Keep it under {events.MAX_NAME} characters"),
            "call_type": st.column_config.SelectboxColumn("Calls", options=list(tmap.values())),
            "line_stop": st.column_config.CheckboxColumn("Line stop (red)"),
            "priority": st.column_config.SelectboxColumn("Priority", options=[1, 2, 3]),
            "show_on_board": st.column_config.CheckboxColumn("Call someone", help="Off = logged and counted in OEE, but no call and no colour on the board"),
            "oee_category": st.column_config.SelectboxColumn("Counts in OEE as", options=list(db.OEE_CATEGORIES)),
            "renamed": st.column_config.CheckboxColumn("Reviewed", disabled=True),
            "renamed_by": st.column_config.TextColumn("Reviewed by", disabled=True)},
            ["id", "source", "code", "raw_text", "short_name", "name_len", "short_name_da", "call_type", "call_type_id", "line_stop",
             "priority", "show_on_board", "oee_category", "renamed", "renamed_by", "renamed_at", "first_seen", "active"], dynamic=False)
        st.caption("OEE categories: " + " · ".join(f"**{k}** = {v}" for k, v in db.OEE_CATEGORIES.items()))
        if st.button("Save event catalogue", type="primary"):
            ed = ed.assign(call_type_id=ed["call_type"].map(trev).astype("Int64"))
            orig = view.set_index("id")
            changed = []
            for _, r in ed.iterrows():
                o = orig.loc[r["id"]]
                if any(str(r[k]) != str(o[k]) for k in ["short_name", "short_name_da", "call_type_id", "line_stop", "priority",
                                                          "show_on_board", "oee_category"]):
                    changed.append(r["id"])
            ed["renamed"] = [True if i in changed else bool(orig.loc[i, "renamed"]) for i in ed["id"]]
            ed["renamed_by"] = [_actor() if i in changed else orig.loc[i, "renamed_by"] for i in ed["id"]]
            ed["renamed_at"] = [db.utcnow() if i in changed else orig.loc[i, "renamed_at"] for i in ed["id"]]
            ed["short_name"] = ed["short_name"].fillna("").str.strip().str[:events.MAX_NAME]
            if (ed["short_name"] == "").any():
                st.error("Every event needs a board name.")
            else:
                _saved(_save(e, db.events, ed.assign(active=True), view,
                             ["short_name", "short_name_da", "call_type_id", "line_stop", "priority", "show_on_board",
                              "oee_category", "renamed", "renamed_by", "renamed_at", "active"]))
        st.caption("The original code and text are never changed, and every rename is in the audit log. Planned stops "
                   "(changeover, cleaning) appear in blue on the board and count as planned time in OEE.")

    # -------------------------------------------------- call types
    with tabs[3]:
        ct = db.read_df(e, db.call_types, active_only=True)
        roles = db.ROLES
        ed = _editor(ct, "ed_ct", {
            "id": None, "active": None, "name": "Call type",
            "color": st.column_config.TextColumn("Colour", help="Hex colour, e.g. #2563eb"),
            "l1_role": st.column_config.SelectboxColumn("Level 1 (immediately)", options=roles, required=True),
            "l2_min": st.column_config.NumberColumn("Level 2 after (min)", min_value=0, step=1),
            "l2_role": st.column_config.SelectboxColumn("Level 2 role", options=roles),
            "l3_min": st.column_config.NumberColumn("Level 3 after (min)", min_value=0, step=1),
            "l3_role": st.column_config.SelectboxColumn("Level 3 role", options=roles),
            "sort": st.column_config.NumberColumn("Order", step=1)},
            ["id", "name", "color", "l1_role", "l2_min", "l2_role", "l3_min", "l3_role", "sort", "active"])
        st.caption("Escalation runs until someone acknowledges the call. Leave level 2 or 3 empty to stop escalating earlier.")
        if st.button("Save call types", type="primary"):
            _saved(_save(e, db.call_types, ed.assign(active=ed["active"].fillna(True), sort=ed["sort"].fillna(0),
                                                     color=ed["color"].fillna("#64748b")), ct,
                         ["name", "color", "l1_role", "l2_min", "l2_role", "l3_min", "l3_role", "sort", "active"]))

    # -------------------------------------------------- reasons
    with tabs[4]:
        rs = db.read_df(e, db.reasons, active_only=True)
        rv = rs.assign(type=rs["call_type_id"].map(tmap)).sort_values(["call_type_id", "id"])
        ed = _editor(rv, "ed_rs", {"id": None, "call_type_id": None, "active": None,
                                   "type": st.column_config.SelectboxColumn("Call type", options=list(tmap.values()), required=True),
                                   "name": "Reason"}, ["id", "call_type_id", "type", "name", "active"])
        st.caption("Reason codes are chosen by responders when they close a call (the root cause), while events describe the symptom.")
        if st.button("Save reason codes", type="primary"):
            ed = ed.assign(call_type_id=ed["type"].map(trev), active=ed["active"].fillna(True))
            _saved(_save(e, db.reasons, ed, rs, ["call_type_id", "name", "active"]))

    # -------------------------------------------------- signals & rules
    with tabs[5]:
        sett = c["settings"]
        sim = st.toggle("Built-in simulator", value=sett.get("simulator") == "1",
                        help="Generates realistic machine data inside the app. The connector switches it off when real data flows.")
        if sim != (sett.get("simulator") == "1"):
            db.set_setting(e, "simulator", "1" if sim else "0")
            db.audit(e, _actor(), "settings_changed", f"simulator {'on' if sim else 'off'}", ui.client_id())
            ui.refresh_cfg()
            st.rerun()
        st.markdown("**Signals per unit**")
        sd = db.read_df(e, db.signal_defs, active_only=True)
        stn = c["stations"].merge(c["lines"][["id", "name"]].rename(columns={"id": "line_id", "name": "line"}), on="line_id")
        smap = {int(r.id): f"{r.line} · {r.name}" for r in stn.itertuples()}
        srev = {v: k for k, v in smap.items()}
        sv = sd.assign(station=sd["station_id"].map(smap))
        ed = _editor(sv, "ed_sd", {"id": None, "station_id": None, "active": None,
                                   "station": st.column_config.SelectboxColumn("Unit", options=list(smap.values()), required=True),
                                   "key": st.column_config.TextColumn("Signal key", help="Name used by the connector, e.g. temperature"),
                                   "label": "Label", "kind": st.column_config.SelectboxColumn("Kind", options=KINDS, required=True),
                                   "unit": "Unit", "lo": st.column_config.NumberColumn("Min"), "hi": st.column_config.NumberColumn("Max"),
                                   "target": st.column_config.NumberColumn("Target", help="Speed: units/min (ideal rate for OEE). Scrap: expected %"),
                                   "sort": st.column_config.NumberColumn("Order", step=1)},
                     ["id", "station_id", "station", "key", "label", "kind", "unit", "lo", "hi", "target", "sort", "active"])
        if st.button("Save signals", type="primary"):
            ed = ed.assign(station_id=ed["station"].map(srev), active=ed["active"].fillna(True), sort=ed["sort"].fillna(0))
            ed["label"] = ed["label"].fillna(ed["key"])
            _saved(_save(e, db.signal_defs, ed, sd, ["station_id", "key", "label", "kind", "unit", "lo", "hi", "target", "sort", "active"]))
        st.markdown("**Rules that raise calls automatically**")
        rl = db.read_df(e, db.rules, active_only=True)
        rv = rl.assign(call_type=rl["call_type_id"].map(tmap), station=rl["station_id"].map(smap))
        ed = _editor(rv, "ed_rl", {"id": None, "call_type_id": None, "station_id": None, "active": None, "name": "Rule",
                                   "signal_key": "Signal key",
                                   "condition": st.column_config.SelectboxColumn("Condition", options=list(CONDITIONS), required=True,
                                                                                 help=" · ".join(f"{k}: {v}" for k, v in CONDITIONS.items())),
                                   "param": "Parameter",
                                   "duration_s": st.column_config.NumberColumn("For at least (s)", min_value=0, step=5),
                                   "call_type": st.column_config.SelectboxColumn("Raises (default)", options=list(tmap.values()), required=True),
                                   "line_stopped": st.column_config.CheckboxColumn("Line stopped"),
                                   "station": st.column_config.SelectboxColumn("Only unit", options=list(smap.values()),
                                                                               help="Empty = every unit with this signal")},
                     ["id", "name", "signal_key", "condition", "param", "duration_s", "call_type", "call_type_id",
                      "line_stopped", "station", "station_id", "active"])
        st.caption("When a rule fires on a machine fault or process alarm, the event catalogue decides the exact problem name, "
                   "the call type and whether it is a line stop. The rule's own call type is the fallback.")
        if st.button("Save rules", type="primary"):
            ed = ed.assign(call_type_id=ed["call_type"].map(trev), station_id=ed["station"].map(srev).astype("Int64"),
                           active=ed["active"].fillna(True), line_stopped=ed["line_stopped"].fillna(False).astype(bool),
                           duration_s=ed["duration_s"].fillna(30))
            _saved(_save(e, db.rules, ed, rl, ["name", "signal_key", "condition", "param", "duration_s", "call_type_id",
                                               "line_stopped", "station_id", "active"]))

    # -------------------------------------------------- OEE model & targets
    with tabs[6]:
        sett = c["settings"]
        st.markdown("**Time model** (from total calendar time to valued operating time)")
        st.markdown("- **TCU** = valued operating time ÷ total calendar time\n"
                    "- **OEE3** = valued ÷ scheduled time (removes time with no production planned)\n"
                    "- **OEE2** = valued ÷ planned production time (also removes planned stops)\n"
                    "- **OEE1** = valued ÷ line-controllable time (also removes external stops)\n"
                    "- **Valued operating time** = good units ÷ target rate. Which stops are planned or external is set per event in the catalogue.")
        with st.form("oee_model"):
            a, b = st.columns(2)
            base = a.selectbox("Headline OEE (A × P × Q) measured on", ["oee2", "oee1"],
                               index=0 if sett.get("oee_headline_base", "oee2") != "oee1" else 1,
                               format_func=lambda k: {"oee2": "Planned production time (equals OEE2, the classic definition)",
                                                      "oee1": "Line-controllable time (equals OEE1, external stops excluded)"}[k])
            micro = b.number_input("Stops shorter than this are microstops (s)", 0, 900, int(sett.get("microstop_s") or 120),
                                   help="Microstops count as performance loss, not as stops.")
            if st.form_submit_button("Save model", type="primary"):
                db.set_setting(e, "oee_headline_base", base)
                db.set_setting(e, "microstop_s", str(micro))
                db.audit(e, _actor(), "settings_changed", f"OEE model: headline {base}, microstop {micro}s", ui.client_id())
                ui.refresh_cfg()
                st.toast("OEE model saved")
                st.rerun()
        st.markdown("**Targets** (empty line = plant default)")
        tg = db.read_df(e, db.oee_targets)
        tv = tg.assign(line=tg["line_id"].map(lambda i: lmap.get(int(i)) if pd.notna(i) else None), target_pct=tg["target"] * 100)
        ed = _editor(tv, "ed_tg", {"id": None, "line_id": None, "target": None,
                                   "line": st.column_config.SelectboxColumn("Line", options=list(lmap.values()), help="Empty = all lines"),
                                   "metric": st.column_config.SelectboxColumn("Metric", options=oee.METRICS, required=True),
                                   "target_pct": st.column_config.NumberColumn("Target %", min_value=0, max_value=100, step=0.5)},
                     ["id", "line", "line_id", "metric", "target_pct", "target"])
        if st.button("Save targets", type="primary"):
            ed = ed.assign(line_id=ed["line"].map(lrev).astype("Int64"), target=ed["target_pct"] / 100)
            ed = ed[ed["metric"].notna() & ed["target"].notna()]
            _saved(_save(e, db.oee_targets, ed, tg, ["line_id", "metric", "target"]))

    # -------------------------------------------------- shifts & schedule
    with tabs[7]:
        sett = c["settings"]
        shifts = json.loads(sett.get("shifts"))
        with st.form("shifts"):
            st.markdown("**Shifts**")
            sh = st.data_editor(pd.DataFrame(shifts, columns=["Shift", "Start", "End"]), num_rows="dynamic", hide_index=True, key="ed_shifts",
                                column_config={"Start": st.column_config.TextColumn(help="HH:MM"), "End": st.column_config.TextColumn(help="HH:MM")})
            st.markdown("**Which shifts each line runs** (time outside them counts as *not scheduled* in OEE)")
            names = [s[0] for s in shifts]
            sched = {}
            for ln in lines.itertuples():
                cur = json.loads(ln.scheduled_shifts) if isinstance(ln.scheduled_shifts, str) and ln.scheduled_shifts else names
                a, b = st.columns([3, 1])
                sched[ln.id] = (a.multiselect(ln.name, names, default=[x for x in cur if x in names], key=f"sch_{ln.id}"),
                                b.checkbox("Weekends", value=bool(ln.weekends), key=f"wk_{ln.id}"))
            if st.form_submit_button("Save shifts and schedule", type="primary"):
                from datetime import time as _t
                from sqlalchemy import update
                try:
                    rows = [[str(a).strip(), str(b).strip(), str(cc).strip()] for a, b, cc in sh.dropna().values.tolist()]
                    for _, b, cc in rows:
                        _t.fromisoformat(b), _t.fromisoformat(cc)
                except Exception as exc:
                    st.error(f"Shift times must be HH:MM. {exc}")
                else:
                    db.set_setting(e, "shifts", json.dumps(rows))
                    with e.begin() as cx:
                        for lid, (sel, wk) in sched.items():
                            cx.execute(update(db.lines).where(db.lines.c.id == lid).values(
                                scheduled_shifts=json.dumps(sel) if sel and len(sel) < len(rows) else None, weekends=bool(wk)))
                        db.audit(cx, _actor(), "settings_changed", "shifts and line schedule", ui.client_id())
                    ui.refresh_cfg()
                    st.toast("Shifts and schedule saved")
                    st.rerun()

    with tabs[8]:
        _notifications_tab(e, c, lmap, lrev)


def _notifications_tab(e, c, lmap, lrev):
    from andon import notify
    st.markdown("**Notifications**: send line stops and escalations to Microsoft Teams, by SMS or to a webhook. "
                "Each message is sent once per call and event. Sending runs in the app, never in the shop-floor connector.")
    ch = db.read_df(e, db.notify_channels, active_only=True)
    roles = db.ROLES
    view = ch.assign(
        role_names=ch["roles"].map(lambda x: ", ".join(json.loads(x)) if isinstance(x, str) and x else ""),
        line_names=ch["lines"].map(lambda x: ", ".join(lmap.get(i, str(i)) for i in json.loads(x)) if isinstance(x, str) and x else ""),
        target_shown=ch["target"].map(notify.mask), new_target="")
    ed = _editor(view, "ed_notify", {
        "id": st.column_config.NumberColumn("Id", disabled=True), "active": None, "roles": None, "lines": None, "target": None,
        "name": "Name",
        "kind": st.column_config.SelectboxColumn("Send by", options=list(notify.KINDS), required=True,
                                                 help="log = nothing leaves the app (demo); teams; webhook; sms (Twilio)"),
        "trigger": st.column_config.SelectboxColumn("When", options=list(notify.TRIGGERS), required=True,
                                                    help="red = line stops · escalation = escalations and stops · all = every new call too"),
        "role_names": st.column_config.TextColumn("Roles", help="Only calls now with these roles, comma-separated: " + ", ".join(roles)
                                                  + ". Empty = any role."),
        "line_names": st.column_config.TextColumn("Lines", help="Comma-separated line names. Empty = all lines."),
        "target_shown": st.column_config.TextColumn("Target (hidden)", disabled=True),
        "new_target": st.column_config.TextColumn("New target", help="Teams/webhook URL, phone numbers, or ${SECRET_NAME}. "
                                                  "Leave empty to keep the current one.")},
        ["id", "name", "kind", "trigger", "role_names", "line_names", "target_shown", "new_target", "target", "roles", "lines", "active"])
    st.caption("Tip: put the real webhook URL or phone numbers in Streamlit secrets (e.g. TEAMS_MAINTENANCE = \"https://…\") and type "
               "**${TEAMS_MAINTENANCE}** as the target, so the address is never stored in the database. "
               "SMS needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM in the secrets.")
    if st.button("Save notifications", type="primary"):
        ed = ed.copy()
        orig = ch.set_index("id")["target"].to_dict() if not ch.empty else {}
        ed["target"] = [(t.strip() if isinstance(t, str) and t.strip() else orig.get(i)) for t, i in zip(ed["new_target"], ed["id"])]
        ed["roles"] = ed["role_names"].map(lambda t: json.dumps([x.strip() for x in t.split(",") if x.strip() in roles])
                                           if isinstance(t, str) and t.strip() else None)
        ed["lines"] = ed["line_names"].map(lambda t: json.dumps([lrev[x.strip()] for x in t.split(",") if x.strip() in lrev])
                                           if isinstance(t, str) and t.strip() else None)
        ed["kind"], ed["trigger"], ed["active"] = ed["kind"].fillna("log"), ed["trigger"].fillna("red"), True
        problems = [f"{r.name}: {p}" for r in ed.itertuples() if isinstance(r.name, str) and (p := notify.check_channel(r.kind, r.target))]
        if problems:
            st.error("Not saved. " + " · ".join(problems))
        else:
            _saved(_save(e, db.notify_channels, ed, ch, ["name", "kind", "trigger", "target", "roles", "lines", "active"]))
    if not ch.empty:
        a, b = st.columns([3, 1], vertical_alignment="bottom")
        pick = a.selectbox("Send a test message to", list(ch["id"].astype(int)), key="notify_test_pick",
                           format_func=lambda i: ch.set_index("id").loc[i, "name"])
        if b.button("Send test", width="stretch"):
            ok, detail = notify.send_test(e, ch.set_index("id").loc[pick].to_dict() | {"id": pick}, _actor(), ui.client_id())
            (st.success if ok else st.error)(f"{'Sent' if ok else 'Failed'}: {detail}")
    app_url = st.text_input("App address used in links", c["settings"].get("app_url") or "",
                            placeholder="https://andon.yourcompany.com", key="notify_app_url")
    if app_url != (c["settings"].get("app_url") or "") and st.button("Save address"):
        db.set_setting(e, "app_url", app_url.strip())
        ui.refresh_cfg()
        st.rerun()
    st.markdown("**Recent notifications**")
    from sqlalchemy import select
    with e.connect() as cx:
        log = pd.read_sql(select(db.notify_log).order_by(db.notify_log.c.id.desc()).limit(50), cx)
    if log.empty:
        st.caption("Nothing sent yet. Messages appear here when a line stops or a call escalates.")
    else:
        names = ch.set_index("id")["name"].to_dict() if not ch.empty else {}
        log["channel"] = log["channel_id"].map(names)
        log["ts"] = pd.to_datetime(log["ts"]).dt.tz_localize("UTC").dt.tz_convert(ui.zone()).dt.strftime("%d %b %H:%M:%S")
        st.dataframe(log[["ts", "channel", "event", "status", "message", "detail"]], hide_index=True, width="stretch",
                     column_config={"ts": "Time", "channel": "Channel", "event": "Event", "status": "Status",
                                    "message": st.column_config.TextColumn("Message", width="large"), "detail": "Result"})


def _boards_tab(e, c, lines, lmap):
    """One board per screen. Line screens show exactly one line (the shop floor); overviews show several lines and
    are only offered to line managers, viewers, engineers and admins."""
    from sqlalchemy import insert, update
    st.markdown("**Andon boards**: one board per screen. A **line screen** shows exactly one line and is what the screen at "
                "that line displays. A **management overview** shows several lines and is only offered to line managers, "
                "viewers (management), engineers and admins.")
    b = db.read_df(e, db.boards, active_only=True)
    key = ui.secret("DEVICE_KEY")
    if not b.empty:
        view = pd.DataFrame({
            "Board": b["name"],
            "Type": b["kind"].fillna("overview").map({"line": "Line screen", "overview": "Management overview"}),
            "Lines": b["lines"].map(lambda x: ", ".join(lmap.get(i, str(i)) for i in json.loads(x)) if isinstance(x, str) and x else "All lines"),
            "Language": b["language"], "Theme": b["theme"].fillna("light"),
            "TV address": b["id"].map(lambda i: f"/board?kiosk=1&b={int(i)}" + ("&key=…" if key else ""))})
        st.dataframe(view, hide_index=True, width="stretch")
    ids = list(b["id"].astype(int)) if not b.empty else []
    names = b.set_index("id")["name"].to_dict() if not b.empty else {}
    pick = st.selectbox("Edit a board", [0] + ids, format_func=lambda i: "➕ New board" if i == 0 else names[i], key="board_edit_pick")
    cur = b.set_index("id").loc[pick].to_dict() if pick else {
        "name": "", "kind": "line", "lines": None, "tile_label": "event", "language": "en", "theme": "light", "show_kpis": True,
        "show_speed": True, "show_process": True, "show_responder": True, "show_ticker": True, "show_recent": True, "sound": True,
        "sort": len(ids)}
    cur_lines = [int(x) for x in json.loads(cur["lines"])] if isinstance(cur.get("lines"), str) and cur["lines"] else []
    line_ids = list(lines["id"].astype(int))
    with st.container(border=True):
        a, bb = st.columns([2, 1.4])
        name = a.text_input("Board name", cur.get("name") or "", key=f"bn_{pick}", placeholder="e.g. Line 2 screen")
        kind = bb.radio("Type", ["line", "overview"], index=0 if cur.get("kind") == "line" else 1, horizontal=True, key=f"bk_{pick}",
                        format_func={"line": "Line screen (one line)", "overview": "Management overview"}.get)
        if kind == "line":
            default = cur_lines[0] if cur_lines and cur_lines[0] in line_ids else (line_ids[0] if line_ids else None)
            one = st.selectbox("Line shown on this screen", line_ids, index=line_ids.index(default) if default in line_ids else 0,
                               format_func=lmap.get, key=f"bl1_{pick}")
            chosen = [one] if one is not None else []
        else:
            chosen = st.multiselect("Lines on this board (remove a line with its ×; empty = all lines)", line_ids,
                                    default=[i for i in cur_lines if i in line_ids], format_func=lmap.get, key=f"blm_{pick}")
        c1, c2, c3 = st.columns(3)
        tile = c1.selectbox("Tile shows", ["event", "call_type", "both"], index=["event", "call_type", "both"].index(cur.get("tile_label") or "event"),
                            format_func={"event": "The exact problem", "call_type": "The call type", "both": "Both"}.get, key=f"bt_{pick}")
        lang = c2.selectbox("Language", ["en", "da"], index=0 if (cur.get("language") or "en") == "en" else 1,
                            format_func={"en": "English", "da": "Dansk"}.get, key=f"bg_{pick}")
        theme = c3.selectbox("Theme", ["light", "dark"], index=0 if (cur.get("theme") or "light") == "light" else 1,
                             format_func={"light": "Light (recommended, colour-blind friendly)", "dark": "Dark"}.get, key=f"bh_{pick}")
        o = st.columns(4)
        opts = {}
        for n, (col, label) in enumerate([("show_kpis", "Shift KPIs"), ("show_speed", "Speed"), ("show_process", "Process values"),
                                          ("show_responder", "Who is coming"), ("show_ticker", "Open-call strip"),
                                          ("show_recent", "Recently fixed"), ("sound", "Sound")]):
            opts[col] = o[n % 4].checkbox(label, value=bool(cur.get(col, True)) if pd.notna(cur.get(col, True)) else True, key=f"bo_{col}_{pick}")
        s1, s2, s3 = st.columns([1, 1, 2], vertical_alignment="bottom")
        if s1.button("Save board", type="primary", width="stretch", key=f"bsave_{pick}"):
            if not name.strip():
                st.error("Give the board a name.")
            elif kind == "line" and len(chosen) != 1:
                st.error("A line screen shows exactly one line.")
            else:
                values = dict(name=name.strip()[:80], kind=kind, lines=json.dumps(chosen) if chosen else None, tile_label=tile,
                              language=lang, theme=theme, sort=int(cur.get("sort") or 0), active=True, **opts)
                with e.begin() as cx:
                    if pick:
                        cx.execute(update(db.boards).where(db.boards.c.id == pick).values(**values))
                    else:
                        cx.execute(insert(db.boards).values(**values))
                    db.audit(cx, _actor(), "config_changed", f"board {'%d' % pick if pick else 'new'}: {values['name']} · {kind} · "
                             f"lines {', '.join(lmap.get(i, str(i)) for i in chosen) or 'all'}", ui.client_id())
                ui.refresh_cfg()
                st.toast("Board saved", icon=":material/save:")
                st.rerun()
        if pick:
            sure = s3.checkbox("Yes, remove this board (its TV address stops working)", key=f"bdelok_{pick}")
            if s2.button("Delete board", disabled=not sure, width="stretch", key=f"bdel_{pick}"):
                with e.begin() as cx:
                    cx.execute(update(db.boards).where(db.boards.c.id == pick).values(active=False))
                    db.audit(cx, _actor(), "config_changed", f"board {pick} removed: {names[pick]}", ui.client_id())
                ui.refresh_cfg()
                st.toast("Board removed")
                st.rerun()
    st.caption("Put line screens on the shop floor and overviews in offices or meeting rooms. A screen shows the board in its "
               "address; people browsing the app only see the boards their access level allows.")
