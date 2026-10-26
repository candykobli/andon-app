"""Account: sign in, sign out, and request access (approved by an admin)."""
from __future__ import annotations

import streamlit as st

from andon import db, ui

REQUESTABLE = ["operator", "responder", "supervisor", "line_manager", "viewer", "engineer"]


def render():
    ui.css()
    c = ui.cfg()
    u = ui.current_user()
    st.markdown("## Account")
    if u:
        st.success(f"Signed in as **{u['name']}** · {db.LEVELS.get(u['level'], u['level'])}"
                   + (" · via company sign-in" if u.get("via") == "sso" else ""), icon=":material/verified_user:")
        names = c["lines"].set_index("id")["name"]
        st.caption("Lines: " + (", ".join(names.get(i, str(i)) for i in u["lines"]) if u.get("lines") else "all lines")
                   + f" · session ends {u['until']:%H:%M}")
        if st.button("Sign out", type="primary"):
            db.audit(ui.engine(), u["name"], "signed_out", "", ui.client_id())
            ui.sign_out()
            if ui.sso_user() is not None:
                st.logout()
            st.rerun()
        st.divider()
        st.markdown("#### Need more access?")
        _request_form(c, prefill_name=u["name"])
        return

    t_in, t_req = st.tabs(["Sign in", "Request access"])
    with t_in:
        if ui.sso_configured():
            st.button("Sign in with your company account", on_click=st.login, type="primary")
            st.caption("Or use your personal PIN:")
        with st.form("signin"):
            pin = st.text_input("PIN", type="password", max_chars=12)
            if st.form_submit_button("Sign in", type="primary"):
                user, msg = ui.sign_in(pin)
                if user:
                    st.rerun()
                st.error(msg or "That PIN is not recognised.")
        if c["settings"].get("demo_mode") == "1":
            st.caption("Demo PINs: Operator 1234 · Responders 2222 / 3333 / 4444 · Supervisor 5555 · Line manager 6666 · "
                       "Process engineer 7777 · Viewer (QA) 8888 · Admin 0000")
    with t_req:
        _request_form(c)


def _request_form(c, prefill_name: str = ""):
    st.caption("An admin reviews every request. You can sign in with your PIN once it is approved.")
    su = ui.sso_user()
    with st.form("access_request", clear_on_submit=True):
        a, b = st.columns(2)
        name = a.text_input("Full name", value=prefill_name or (getattr(su, "name", "") or ""))
        email = b.text_input("Work email", value=getattr(su, "email", "") or "")
        a, b = st.columns(2)
        lvl = a.selectbox("Access needed", REQUESTABLE, index=REQUESTABLE.index("supervisor"),
                          format_func=lambda k: db.LEVELS[k])
        lines = b.multiselect("Lines", list(c["lines"]["id"]), placeholder="All lines",
                              format_func=lambda i: c["lines"].set_index("id").loc[i, "name"])
        why = st.text_area("Why do you need it?", placeholder="e.g. Process engineer for the packaging hall; I set up the event catalogue.")
        a, b = st.columns(2)
        pin1 = a.text_input("Choose a PIN (4–12 digits)", type="password", max_chars=12)
        pin2 = b.text_input("Repeat the PIN", type="password", max_chars=12)
        if st.form_submit_button("Send request", type="primary"):
            if not name.strip() or not why.strip():
                st.error("Fill in your name and the reason.")
            elif not pin1.isdigit() or not 4 <= len(pin1) <= 12:
                st.error("The PIN must be 4–12 digits.")
            elif pin1 != pin2:
                st.error("The two PINs are different.")
            elif pin1 in {"0000", "1111", "1234", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "12345", "123456"}:
                st.error("Choose a less common PIN.")
            else:
                db.request_access(ui.engine(), name, email, lvl, [int(x) for x in lines] or None, why, pin1, ui.client_id())
                st.success("Request sent. An admin will review it; you will be able to sign in once it is approved.")
