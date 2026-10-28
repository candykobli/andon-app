"""Responder view (phone-friendly): see open calls, acknowledge and resolve them with a PIN."""
from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from andon import db, logic, signals, ui

ss = st.session_state
REMEMBER_H = 8


def _me():
    """The signed-in user, if their access level lets them respond to calls."""
    u = ui.current_user()
    return u if u and ui.can("respond") else None


def _login(pin: str):
    u, msg = ui.sign_in(pin)
    if u and not ui.can("respond"):
        ss["pin_msg"] = f"Signed in as {u['name']}, but your access level does not include responding to calls."
        return None
    if not u:
        ss["pin_msg"] = msg
    return u


def render():
    ui.require("staff")
    ui.css()
    c = ui.cfg()
    me = _me()
    u = ui.current_user()
    if u and not me:
        ui.need("respond", "Responder")
    top_l, top_r = st.columns([3, 1.4], vertical_alignment="center")
    top_l.markdown("## Responder")
    with top_r:
        if me:
            st.markdown(f"Signed in as **{me['name']}** · {me['role']}")
            if st.button("Sign out", type="tertiary"):
                ui.sign_out()
                st.rerun()
        else:
            with st.popover("Sign in with PIN", width="stretch"):
                with st.form("login", border=False):
                    pin = st.text_input("PIN", type="password", max_chars=8)
                    if st.form_submit_button("Sign in", type="primary", width="stretch"):
                        if _login(pin):
                            st.rerun()
                        else:
                            st.error(ss.pop("pin_msg", "That PIN is not recognised."))
                ui.demo_pin_hint()

    types = c["call_types"]
    f1, f2 = st.columns([2, 1])
    default_types = list(types["id"])
    if me:  # by default show the calls where the signed-in role is in the escalation chain
        mine = [int(r.id) for r in types.itertuples() if me["role"] in (r.l1_role, r.l2_role, r.l3_role)]
        default_types = mine or default_types
    sel_types = f1.multiselect("Call types", list(types["id"]), default=default_types, key="resp_types",
                               format_func=lambda i: types.set_index("id").loc[i, "name"])
    sel_lines = f2.multiselect("Lines", ui.allowed_lines(c), key="resp_lines", placeholder="All lines",
                               format_func=lambda i: c["lines"].set_index("id").loc[i, "name"])

    left, right = st.columns([1.3, 1], gap="large")
    with left:
        ui.live_control("responder")
        ui.run_live(_queue, sel_types, sel_lines)
    with right:
        _action_panel(c)


def _queue(sel_types, sel_lines):
    e = ui.engine()
    signals.simulate_if_due(e)
    c = ui.cfg()
    oc = logic.enrich_open(db.open_calls(e), c, db.utcnow())
    if not oc.empty:
        if sel_types:
            oc = oc[oc["call_type_id"].isin(sel_types)]
        oc = oc[oc["line_id"].isin(sel_lines or ui.allowed_lines(c))]
    evn = c["events"].set_index("id")["short_name"].to_dict() if not c["events"].empty else {}
    st.markdown(f"#### Open calls ({len(oc)})")
    if oc.empty:
        st.markdown("<span class='an-chip an-green'>Nothing waiting</span>", unsafe_allow_html=True)
        return
    for r in oc.itertuples():
        with st.container(border=True):
            a, b = st.columns([3, 1], vertical_alignment="center")
            chip = "an-red" if r.stopped else "an-yellow"
            auto = " <span class='an-chip an-blue'>Auto</span>" if getattr(r, "source", "manual") == "auto" else ""
            lvl = f" <span class='an-chip an-red'>Level {r.level}</span>" if r.level > 1 and r.status == "open" else ""
            a.markdown(f"<span class='an-chip {chip}'>{'Line stopped' if r.stopped else 'Running'}</span>{auto}{lvl} "
                       f"**{evn.get(r.event_id, r.type) if r.event_id == r.event_id else r.type}** · {r.line} · {r.station}"
                       f" <span class='an-muted'>({r.type})</span>", unsafe_allow_html=True)
            status = f"Acknowledged by {r.ack_by}" if r.status == "ack" else f"Waiting for {r.role}"
            a.caption(f"Open {logic.fmt_dur(r.age_s)} · {status}" + (f" · {r.note}" if isinstance(r.note, str) and r.note else ""))
            if b.button("Open", key=f"sel_{r.id}", width="stretch", type="primary" if ss.get("sel_call") == r.id else "secondary"):
                ss["sel_call"] = int(r.id)
                st.rerun(scope="app")


def _action_panel(c):
    cid = ss.get("sel_call")
    e = ui.engine()
    oc = logic.enrich_open(db.open_calls(e), c, db.utcnow())
    row = oc[oc["id"] == cid] if cid and not oc.empty else None
    if row is None or row.empty:
        st.markdown("#### Select a call")
        st.caption("Tap **Open** on a call to acknowledge or resolve it.")
        if cid:
            ss.pop("sel_call", None)
        return
    r = row.iloc[0]
    with st.container(border=True):
        evrow = c["events"].set_index("id").loc[int(r["event_id"])] if pd.notna(r.get("event_id")) and int(r["event_id"]) in set(c["events"]["id"]) else None
        st.markdown(f"#### {evrow['short_name'] if evrow is not None else r['type']} · {r['line']} · {r['station']}")
        if evrow is not None:
            st.caption(f"{r['type']} · source: {evrow['source']} · code {evrow['code']}"
                       + (f" · original text: {evrow['raw_text']}" if isinstance(evrow['raw_text'], str) and evrow['raw_text'] != evrow['short_name'] else ""))
        st.caption(f"Raised {logic.fmt_dur(r['age_s'])} ago" + (" by a machine rule" if r.get("source") == "auto" else "")
                   + (" · line stopped" if r["stopped"] else " · line running"))
        if isinstance(r["note"], str) and r["note"]:
            st.info(r["note"], icon=":material/sticky_note_2:")
        me = _me()
        if r["status"] == "open":
            with st.form(f"ack_{cid}", border=False):
                pin = "" if me else st.text_input("PIN", type="password", key=f"ackpin_{cid}")
                if st.form_submit_button(f"Acknowledge{' as ' + me['name'] if me else ''}", type="primary", width="stretch"):
                    who = me or _login(pin)
                    if not who:
                        st.error(ss.pop("pin_msg", "That PIN is not recognised."))
                    elif db.acknowledge(e, int(cid), who["name"], ui.client_id()):
                        st.toast("Acknowledged. The station and the board show you are on the way.", icon=":material/check:")
                        st.rerun()
                    else:
                        st.warning("Someone else acknowledged this call a moment ago.")
                        st.rerun()
        st.markdown("**Resolve**")
        reasons = c["reasons"][c["reasons"]["call_type_id"] == r["call_type_id"]]
        with st.form(f"res_{cid}", border=False):
            reason = st.selectbox("Reason", list(reasons["id"]), index=None, placeholder="Choose the cause",
                                  format_func=lambda i: reasons.set_index("id").loc[i, "name"])
            note = st.text_area("What was done (optional)", height=80)
            pin = "" if me else st.text_input("PIN", type="password", key=f"respin_{cid}")
            if st.form_submit_button(f"Resolve call{' as ' + me['name'] if me else ''}", width="stretch"):
                who = me or _login(pin)
                if reason is None:
                    st.error("Choose a reason so the call counts in the analysis.")
                elif not who:
                    st.error(ss.pop("pin_msg", "That PIN is not recognised."))
                elif db.resolve(e, int(cid), who["name"], int(reason), note, ui.client_id()):
                    ss.pop("sel_call", None)
                    st.toast("Call resolved. The station turns green on the board.", icon=":material/task_alt:")
                    st.rerun()
                else:
                    st.warning("This call was already closed.")
        if not me:
            ui.demo_pin_hint()
