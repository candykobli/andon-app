"""Operator station: raise a call in two taps, see the station's live machine data and open calls."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from andon import db, logic, signals, ui

ss = st.session_state


def _pick_station(c):
    st.subheader("Set up this station")
    st.caption("Choose the station this tablet belongs to. The choice is saved in the page address (/?station=ID), "
               "so you can bookmark it or pin it as the tablet's start page.")
    lines = c["lines"]
    line = st.selectbox("Line", lines["id"], format_func=lambda i: lines.set_index("id").loc[i, "name"])
    sts = c["stations"][c["stations"]["line_id"] == line]
    if sts.empty:
        st.warning("This line has no stations yet. Add them under Admin.")
        return
    station = st.selectbox("Station", sts["id"], format_func=lambda i: sts.set_index("id").loc[i, "name"])
    if st.button("Use this station on this device", type="primary"):
        st.query_params["station"] = str(int(station))
        st.rerun()
    st.caption("If a device key is set (Admin → Security), keep the ?key=… part of the address when you bookmark it.")


def _button_css(c) -> str:
    rules = []
    for r in c["call_types"].itertuples():
        rules.append(f".st-key-call_{r.id} button {{background:{r.color}; color:#fff; border:0; min-height:118px;"
                     f" border-radius:14px;}}"
                     f".st-key-call_{r.id} button p {{font-size:1.4rem !important; font-weight:700 !important; line-height:1.25}}"
                     f".st-key-call_{r.id} button:hover {{filter:brightness(1.08); color:#fff; border:0}}"
                     f".st-key-call_{r.id} button:disabled {{opacity:.45}}")
    return "<style>" + "".join(rules) + "</style>"


def _who() -> str | None:
    u = ui.current_user()
    return u["name"] if u else None


def _raise(line_id, sid, type_id, type_name, stopped, event_id=None):
    note = ss.get(f"note_{sid}", "")
    cid = db.raise_call(ui.engine(), line_id, sid, type_id, stopped, note, client=ui.client_id(), actor=_who(), event_id=event_id)
    if cid:
        st.toast(f"{type_name} called{' · line stopped' if stopped else ''}", icon=":material/campaign:")
        ss[f"note_{sid}"] = ""
    else:
        st.toast(f"{type_name} is already called at this station", icon=":material/info:")


def render():
    ui.require("floor")
    c = ui.cfg()
    ui.css(_button_css(c))
    sid = st.query_params.get("station")
    try:
        sid = int(sid) if sid else None
    except ValueError:
        sid = None
    if sid is None or sid not in set(c["stations"]["id"]):
        _pick_station(c)
        return
    srow = c["stations"].set_index("id").loc[sid]
    line_id = int(srow["line_id"])
    line_name = c["lines"].set_index("id").loc[line_id, "name"]

    head, side = st.columns([3, 1], vertical_alignment="bottom")
    head.markdown(f"## {line_name} · {srow['name']}")
    with side.popover("Change station", width="stretch"):
        if st.button("Choose another station", width="stretch"):
            del st.query_params["station"]
            st.rerun()

    left, right = st.columns([1.15, 1], gap="large")
    with left:
        st.markdown("#### Raise a call")
        stopped = st.segmented_control("Is the line stopped?", ["Line running", "Line stopped"], default="Line running",
                                       key=f"stop_{sid}", width="stretch") == "Line stopped"
        st.text_input("Note (optional)", key=f"note_{sid}", placeholder="e.g. gripper not closing")
        oc = db.open_calls(ui.engine())
        here = oc[oc["station_id"] == sid] if not oc.empty else oc
        open_types = set(here["call_type_id"]) if not here.empty else set()
        types = c["call_types"]
        for i in range(0, len(types), 2):
            cols = st.columns(2)
            for col, t in zip(cols, list(types.itertuples())[i:i + 2]):
                label = t.name + ("\n\nalready called" if t.id in open_types else "")
                col.button(label, key=f"call_{t.id}", width="stretch", disabled=t.id in open_types,
                           on_click=_raise, args=(line_id, sid, int(t.id), t.name, stopped))
        if stopped:
            st.caption(":red-badge[Line stopped] The call shows red on the board and downtime is counted until it is resolved.")
        else:
            st.caption(":orange-badge[Running] The call shows yellow on the board. Switch to *Line stopped* if production has stopped.")
        _specific_problem(c, line_id, sid, stopped, open_types)
        _open_calls(c, sid)
        _planned_stop(c, line_id, sid)
    with right:
        _batch_card(c, line_id)
        _live_panel(c, sid)


def _specific_problem(c, line_id, sid, stopped, open_types):
    """Optional: report the exact problem, so the board shows it instead of only the call type."""
    ev = c["events"]
    if ev.empty:
        return
    choices = ev[ev["show_on_board"].astype(bool) & (ev["oee_category"] != "planned") & ev["call_type_id"].notna()
                 & ~ev["code"].str.startswith("call:") & ~ev["code"].str.startswith("rule:")]
    if choices.empty:
        return
    with st.expander("Report a specific problem (optional)"):
        tnames = c["call_types"].set_index("id")["name"].to_dict()
        eid = st.selectbox("Problem", list(choices["id"]), index=None, placeholder="Choose what is wrong", key=f"prob_{sid}",
                           format_func=lambda i: f"{choices.set_index('id').loc[i, 'short_name']} ({tnames.get(int(choices.set_index('id').loc[i, 'call_type_id']), '')})")
        if eid is not None:
            row = choices.set_index("id").loc[eid]
            tid = int(row["call_type_id"])
            stop = stopped or bool(row["line_stop"])
            st.button(f"Call {tnames.get(tid, 'help')}: {row['short_name']}" + (" · line stopped" if stop else ""), type="primary",
                      disabled=tid in open_types, key=f"probbtn_{sid}", width="stretch",
                      on_click=_raise, args=(line_id, sid, tid, tnames.get(tid, "Call"), stop, int(eid)))


def _planned_stop(c, line_id, sid):
    """Changeover, cleaning, planned maintenance: shown blue on the board, counted as planned time in OEE."""
    if not ui.can("planned_stop") and ui.current_user() is not None:
        return
    e = ui.engine()
    occ = signals.open_occurrences(e)
    mine = occ[(occ["station_id"] == sid) & (occ["oee_category"] == "planned")] if not occ.empty else occ
    st.markdown("#### Planned stop")
    if not mine.empty:
        r = mine.iloc[0]
        dur = (db.utcnow() - r["started_at"]).total_seconds()
        st.markdown(f"<span class='an-chip an-blue'>{r['short_name']}</span> running for **{logic.fmt_dur(dur)}**", unsafe_allow_html=True)
        if str(r["code"]).startswith("OP-") and st.button("End planned stop", key=f"endpl_{sid}"):
            signals.ingest(e, [{"station_id": sid, "key": "fault_code", "value": ""}, {"station_id": sid, "key": "state", "value": "Running"}])
            db.audit(e, _who() or f"station {sid}", "planned_stop_ended", f"station {sid} · {r['short_name']}", ui.client_id())
            st.rerun()
        return
    planned = c["events"][(c["events"]["oee_category"] == "planned") & c["events"]["code"].str.startswith("OP-")]
    if planned.empty:
        st.caption("No planned-stop reasons are configured (Configure → Event catalogue, codes starting with OP-).")
        return
    a, b = st.columns([3, 1.2], vertical_alignment="bottom")
    pid = a.selectbox("Reason", list(planned["id"]), key=f"pl_{sid}", format_func=lambda i: planned.set_index("id").loc[i, "short_name"])
    if b.button("Start", key=f"startpl_{sid}", width="stretch"):
        raw = planned.set_index("id").loc[pid, "raw_text"]
        signals.ingest(e, [{"station_id": sid, "key": "fault_code", "value": raw}, {"station_id": sid, "key": "state", "value": "Planned"}])
        db.audit(e, _who() or f"station {sid}", "planned_stop_started", f"station {sid} · {raw}", ui.client_id())
        st.rerun()
    st.caption("Use this for changeovers, cleaning or planned maintenance, so they are not counted as breakdowns.")


def _batch_card(c, line_id):
    from sqlalchemy import select
    with ui.engine().connect() as cx:
        r = cx.execute(select(db.batches).where(db.batches.c.line_id == line_id, db.batches.c.ended_at.is_(None))
                       .order_by(db.batches.c.started_at.desc()).limit(1)).mappings().first()
    if r:
        st.markdown(f"<div style='border:1px solid rgba(128,128,128,.3);border-radius:10px;padding:8px 12px;margin-bottom:8px'>"
                    f"<span class='an-muted'>Running batch</span><br><b>{r['batch_no']}</b> · {r['product']}"
                    f"<br><span class='an-muted'>Destination: {r['destination'] or '–'}</span></div>", unsafe_allow_html=True)


@st.fragment(run_every="5s")
def _open_calls(c, sid):
    e = ui.engine()
    signals.simulate_if_due(e)
    now = db.utcnow()
    oc = logic.enrich_open(db.open_calls(e), c, now)
    here = oc[oc["station_id"] == sid] if not oc.empty else oc
    evn = c["events"].set_index("id")["short_name"].to_dict() if not c["events"].empty else {}
    st.markdown("#### Open calls at this station")
    if here.empty:
        st.markdown("<span class='an-chip an-green'>No open calls</span>", unsafe_allow_html=True)
        return
    window = int(c["settings"].get("cancel_window_min") or 2)
    for r in here.itertuples():
        with st.container(border=True):
            a, b = st.columns([3, 1.2], vertical_alignment="center")
            chip = "an-red" if r.stopped else "an-yellow"
            src = " <span class='an-chip an-blue'>Auto</span>" if getattr(r, "source", "manual") == "auto" else ""
            a.markdown(f"<span class='an-chip {chip}'>{'Line stopped' if r.stopped else 'Running'}</span>{src} "
                       f"**{evn.get(r.event_id, r.type) if r.event_id == r.event_id else r.type}** · open for **{logic.fmt_dur(r.age_s)}**",
                       unsafe_allow_html=True)
            if r.status == "ack":
                a.caption(f"{r.ack_by} is on the way (acknowledged).")
            else:
                msg = f"Waiting for {r.role}"
                if r.level > 1:
                    msg += f" · escalated to level {r.level}"
                if r.next_esc:
                    msg += f" · escalates to {r.next_esc[0]} in {logic.fmt_dur(r.next_esc[1] * 60)}"
                a.caption(msg)
            if isinstance(r.note, str) and r.note:
                a.caption(r.note)
            if not r.stopped and b.button("Line has stopped", key=f"stp_{r.id}", width="stretch"):
                db.mark_stopped(e, int(r.id), ui.client_id())
                st.rerun(scope="app")
            if r.status == "open" and getattr(r, "source", "manual") == "manual" and r.age_s <= window * 60:
                if b.button("Cancel (mistake)", key=f"cnl_{r.id}", type="tertiary", width="stretch"):
                    db.cancel(e, int(r.id), window, ui.client_id())
                    st.rerun(scope="app")


@st.fragment(run_every="5s")
def _live_panel(c, sid):
    e = ui.engine()
    now = db.utcnow()
    defs = db.read_df(e, db.signal_defs, active_only=True)
    snap = signals.station_snapshot(signals.latest_df(e), defs, now).get(sid, {})
    st.markdown("#### Machine")
    if not snap:
        st.info("No machine data for this station yet. Connect a signal source (see the README) or switch on the simulator in Configure.")
        return
    state = snap.get("state", {})
    sv = state.get("value") or "No data"
    if state.get("age_s", 1e9) > signals.STALE_S:
        sv = "No data"
    chip = {"Running": "an-green", "Idle": "an-yellow", "Stopped": "an-red", "Fault": "an-red", "Planned": "an-blue"}.get(sv, "an-blue")
    sv = {"Off": "Not scheduled"}.get(sv, sv)
    fc = snap.get("fault_code", {}).get("value")
    if fc:
        from andon import events as _ev
        code = _ev.parse_code(fc)[0]
        hit = c["events"][c["events"]["code"] == code]
        fc = hit.iloc[0]["short_name"] if not hit.empty else fc
    st.markdown(f"<span class='an-chip {chip}' style='font-size:1rem;padding:.3rem .9rem'>{sv}</span> "
                f"<span class='an-muted'>for {logic.fmt_dur(state.get('since_s'))}</span>"
                + (f"<br><span class='an-muted'>{fc}</span>" if fc else ""), unsafe_allow_html=True)
    sp = snap.get("speed")
    m1, m2, m3 = st.columns(3)
    if sp:
        tgt = sp.get("target")
        m1.metric("Speed", f"{sp['value']:.1f}" if isinstance(sp["value"], float) else "–",
                  f"{(sp['value'] / tgt - 1) * 100:+.0f}% vs {tgt:g}" if tgt and isinstance(sp["value"], float) else None,
                  help=f"{sp.get('unit') or ''} · target {tgt:g}" if tgt else None, border=True)
    g, s_ = snap.get("good", {}).get("value"), snap.get("scrap", {}).get("value")
    if isinstance(g, float):
        m2.metric("Good this shift", f"{int(g):,}", border=True)
        tot = g + (s_ or 0)
        tgt_scrap = snap.get("scrap", {}).get("target")
        rate = (s_ or 0) / tot * 100 if tot else 0
        m3.metric("Scrap", f"{rate:.1f}%", f"{rate - tgt_scrap:+.1f} pts vs {tgt_scrap:g}%" if tgt_scrap else None,
                  delta_color="inverse", help=f"{int(s_ or 0):,} units rejected", border=True)
    proc = [(k, v) for k, v in snap.items() if v.get("kind") == "process"]
    if proc:
        cols = st.columns(len(proc))
        for col, (k, v) in zip(cols, proc):
            lim = " – ".join(f"{x:g}" for x in (v.get("lo"), v.get("hi")) if x is not None and not pd.isna(x))
            val = f"{v['value']:.1f} {v.get('unit') or ''}" if isinstance(v["value"], float) else "–"
            col.metric(v["label"], val, "outside limits" if v["alarm"] else None, delta_color="inverse" if v["alarm"] else "off",
                       help=f"Limits {lim}" if lim else None, border=True)
    hist = signals.history_df(e, now - pd.Timedelta(minutes=45), [sid], ["speed"])
    if not hist.empty:
        zone = ui.zone()
        x = hist["ts"].dt.tz_localize(logic.UTC).dt.tz_convert(zone)
        fig = go.Figure(go.Scatter(x=x, y=hist["num"], mode="lines", line=dict(width=2, color="#2563eb"), name="Speed"))
        if sp and sp.get("target"):
            fig.add_hline(y=sp["target"], line_dash="dot", line_color="#16a34a", annotation_text="target")
        fig.update_layout(height=170, margin=dict(l=4, r=4, t=6, b=4), showlegend=False, yaxis_title=None)
        fig.update_xaxes(tickformat="%H:%M")
        st.caption("Speed, last 45 minutes")
        st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"spk_{sid}")
