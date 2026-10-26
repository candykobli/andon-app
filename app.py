"""Andon — shop-floor call system, OEE board and live machine signals.

Run locally:  streamlit run app.py
Settings (Streamlit secrets or environment variables):
  DATABASE_URL   any PostgreSQL URL (Supabase, Neon, Azure, AWS RDS, on-premise). Empty = local SQLite file.
  SEED_DEMO      "1" (default) seeds a demo plant on first start; "0" starts empty.
  DEVICE_KEY     tablets and TV screens only open with ?key=<value>.
  [auth]         optional company sign-in (OpenID Connect), see SECURITY.md.

Screens by access level
  everyone on the shop floor   Operator, Andon board, OEE board (operator view), Account (sign in / request access)
  responder                    + Responder, Machines
  supervisor                   + Analytics, OEE supervisor view, batches and handover notes
  line manager / viewer        + OEE full view (TCU, OEE1-3, waterfall)
  process engineer / BA / IT   + Configure (boards, units, event catalogue, rules, OEE model)
  admin                        + Admin (access requests, users, security, settings)
TV screens (no menu):  /board?kiosk=1&b=<board id>   and   /oee?kiosk=1&lines=1,2
"""
import streamlit as st

from andon import ui
from views import account, admin, analytics, board, configure, machines, oee_board, operator, responder

st.set_page_config(page_title="Andon", page_icon=":material/emergency_heat:", layout="wide")

ui.engine()  # connect, create or upgrade tables on first run


def _tv_home():
    st.markdown("## TV screens")
    st.markdown("Open one of these addresses on the wall screen:\n\n"
                "- Andon board: `/board?kiosk=1&b=<board id>` (board ids are under Configure → Boards)\n"
                "- OEE board: `/oee?kiosk=1&lines=1,2`")

P = {
    "operator": st.Page(operator.render, title="Operator", icon=":material/touch_app:", default=True),  # served at / (tablets: /?station=ID)
    "board": st.Page(board.render, title="Andon board", icon=":material/tv:", url_path="board"),
    "oee": st.Page(oee_board.render, title="OEE board", icon=":material/speed:", url_path="oee"),
    "responder": st.Page(responder.render, title="Responder", icon=":material/support_agent:", url_path="responder"),
    "machines": st.Page(machines.render, title="Machines", icon=":material/precision_manufacturing:", url_path="machines"),
    "analytics": st.Page(analytics.render, title="Analytics", icon=":material/insights:", url_path="analytics"),
    "configure": st.Page(configure.render, title="Configure", icon=":material/tune:", url_path="configure"),
    "admin": st.Page(admin.render, title="Admin", icon=":material/admin_panel_settings:", url_path="admin"),
    "account": st.Page(account.render, title="Account", icon=":material/person:", url_path="account"),
}
ui.PAGES.update(P)

kiosk = st.query_params.get("kiosk") == "1"
if kiosk:
    # Wall-mounted TV: only the boards, no menu, no configuration.
    # The first page of a navigation is served at "/", so a small start page keeps /board and /oee addressable.
    nav = st.navigation([st.Page(_tv_home, title="TV screens", url_path="tv"), P["board"], P["oee"]], position="hidden")
else:
    visible = [P["operator"], P["board"], P["oee"]]
    if ui.can("respond"):
        visible.append(P["responder"])
    if ui.can("machines"):
        visible.append(P["machines"])
    if ui.can("analytics"):
        visible.append(P["analytics"])
    if ui.can("configure"):
        visible.append(P["configure"])
    if ui.can("admin"):
        visible.append(P["admin"])
    # Pages a viewer may not use are not registered at all, so their addresses do not open for them.
    u = ui.current_user()
    acct = st.Page(account.render, title=u["name"].split(" ")[0] if u else "Sign in", icon=":material/person:", url_path="account")
    ui.PAGES["account"] = acct
    nav = st.navigation(visible + [acct], position="top")
nav.run()
