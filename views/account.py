"""Account: sign in, sign out, and request access (approved by an admin)."""
from __future__ import annotations

import streamlit as st

from andon import db, ui

REQUESTABLE = ["operator", "responder", "supervisor", "line_manager", "viewer", "engineer"]


def render():
    ui.css()
    c = ui.cfg()
    u = ui.current_user()
    if ui.private() and not u:
        return _welcome(c)
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


def _welcome(c):
    """Private demo: the only page anonymous visitors can open."""
    st.markdown("## Andon + OEE · private demo")
    st.markdown("A shop-floor call system with live machine signals, one screen per line and an OEE board with a "
                "different view for each role. It runs on simulated data from a demo plant.")
    st.markdown("Access is approved personally. Send a request below; once it is approved you will be told, and you can sign in "
                "with the PIN you chose here.")
    if not ui.secret("ADMIN_PIN"):
        st.warning("Setup: add ADMIN_PIN (at least 8 digits) to the app's secrets so the owner can approve requests.")
    t_req, t_in = st.tabs(["Request access", "Sign in"])
    with t_req:
        _request_form(c)
    with t_in:
        with st.form("signin"):
            pin = st.text_input("Your PIN", type="password", max_chars=12)
            if st.form_submit_button("Sign in", type="primary"):
                user, msg = ui.sign_in(pin)
                if user:
                    st.rerun()
                st.error(msg or "That PIN is not recognised, or the request has not been approved yet.")
    st.caption("Privacy: your name, email and reason are only used to decide on your request and are visible to the app's "
               "owner. Ask for them to be deleted at any time.")


def _request_form(c, prefill_name: str = ""):
    private = ui.private() and not ui.current_user()
    min_pin = 6 if ui.private() else 4
    st.caption("An admin reviews every request. You can sign in with your PIN once it is approved.")
    su = ui.sso_user()
    with st.form("access_request", clear_on_submit=True):
        a, b = st.columns(2)
        name = a.text_input("Full name", value=prefill_name or (getattr(su, "name", "") or ""))
        email = b.text_input("Work email", value=getattr(su, "email", "") or "")
        a, b = st.columns(2)
        lvl = a.selectbox("Access needed", REQUESTABLE, index=REQUESTABLE.index("line_manager" if private else "supervisor"),
                          format_func=lambda k: db.LEVELS[k],
                          help="Line manager sees nearly everything; process engineer can also change the configuration.")
        if private:
            org = b.text_input("Company or organisation")
            lines = []
        else:
            org = ""
            lines = b.multiselect("Lines", list(c["lines"]["id"]), placeholder="All lines",
                                  format_func=lambda i: c["lines"].set_index("id").loc[i, "name"])
        why = st.text_area("Why do you need it?" if not private else "Why would you like to see the demo?",
                           placeholder="e.g. Process engineer for the packaging hall; I set up the event catalogue." if not private
                           else "e.g. I lead a digital manufacturing team and saw the post on LinkedIn.")
        a, b = st.columns(2)
        pin1 = a.text_input(f"Choose a PIN ({min_pin}–12 digits)", type="password", max_chars=12)
        pin2 = b.text_input("Repeat the PIN", type="password", max_chars=12)
        if st.form_submit_button("Send request", type="primary"):
            e = ui.engine()
            if not name.strip() or not why.strip() or (private and "@" not in (email or "")):
                st.error("Fill in your name, " + ("a valid email " if private else "") + "and the reason.")
            elif ui.private() and (db.recent_requests(e, ui.client_id(), 60) >= 3 or db.recent_requests(e, None, 60) >= 60):
                st.error("Too many requests just now. Please try again later.")
            elif not pin1.isdigit() or not min_pin <= len(pin1) <= 12:
                st.error(f"The PIN must be {min_pin}–12 digits.")
            elif pin1 != pin2:
                st.error("The two PINs are different.")
            elif pin1 in {"0000", "1111", "1234", "2222", "3333", "4444", "5555", "6666", "7777", "8888", "9999", "12345", "123456"}:
                st.error("Choose a less common PIN.")
            elif len(set(pin1)) == 1 or pin1 in "0123456789012" or pin1 in "9876543210":
                st.error("Choose a less predictable PIN.")
            else:
                note = (f"[{org.strip()}] " if org and org.strip() else "") + why.strip()
                rid = db.request_access(e, name, email, lvl, [int(x) for x in lines] or None, note, pin1, ui.client_id())
                try:
                    from andon import notify
                    notify.access_request(e, rid, name.strip(), email.strip(), lvl, note)
                except Exception:
                    pass
                st.success("Request sent. You will be able to sign in with your PIN once it is approved.")
