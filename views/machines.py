"""Live machine signals for every station, OEE for the current shift and signal trends."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from andon import db, logic, signals, ui

COLORS = ["#2563eb", "#e0861a", "#17907c", "#c2457a", "#7457c4", "#5c8a25"]


def render():
    ui.require("staff")
    ui.need("machines", "Machines")
    ui.css()
    st.markdown("## Machines")
    ui.live_control("machines")
    ui.run_live(_live)
    st.divider()
    _trends()


def _live():
    e = ui.engine()
    signals.simulate_if_due(e)
    c = ui.cfg()
    now = db.utcnow()
    defs = db.read_df(e, db.signal_defs, active_only=True)
    latest = signals.latest_df(e)
    snap = signals.station_snapshot(latest, defs, now)
    sim = c["settings"].get("simulator") == "1"
    newest = latest["ts"].max() if not latest.empty else None
    age = (now - newest).total_seconds() if newest is not None else None
    if age is None or age > signals.STALE_S:
        st.warning("No live machine data is arriving. Start the connector on the factory network (see README), "
                   "or switch on the simulator under Admin → Signals.", icon=":material/sensors_off:")
    else:
        st.caption(f"Source: {'built-in simulator' if sim else 'connector'} · last reading {age:.0f} s ago · refreshes every 5 s")

    now_l = ui.now_local()
    _, s_start, _ = logic.current_shift_window(now_l, ui.shifts())
    from andon import oee as oee_mod
    oee = oee_mod.station_metrics(e, s_start.astimezone(logic.UTC).replace(tzinfo=None), now + pd.Timedelta(hours=1),
                                  c["settings"].get("oee_headline_base", "oee2"))
    oee = oee.set_index("station_id") if not oee.empty else oee

    rows = []
    lines = c["lines"].set_index("id")["name"]
    for s in c["stations"].itertuples():
        sig = snap.get(int(s.id), {})
        stt = sig.get("state", {})
        stale = not stt or stt.get("age_s", 1e9) > signals.STALE_S
        sp = sig.get("speed", {})
        g = sig.get("good", {}).get("value")
        sc = sig.get("scrap", {}).get("value")
        proc = [f"{v['label']}: {v['value']:.1f} {v.get('unit') or ''}{' ⚠' if v['alarm'] else ''}"
                for v in sig.values() if v.get("kind") == "process" and isinstance(v.get("value"), float)]
        o = oee.loc[s.id] if (not oee.empty and s.id in oee.index) else None
        rows.append({
            "Line": lines.get(s.line_id), "Station": s.name,
            "State": "No data" if stale else stt.get("value"),
            "For": logic.fmt_dur(stt.get("since_s")) if not stale else "",
            "Fault": sig.get("fault_code", {}).get("value") or "",
            "Speed %": (sp["value"] / sp["target"] * 100) if isinstance(sp.get("value"), float) and sp.get("target") else None,
            "Good": int(g) if isinstance(g, float) else None,
            "Scrap %": (sc / (g + sc) * 100) if isinstance(g, float) and isinstance(sc, float) and (g + sc) else None,
            "OEE %": (o["oee"] * 100) if o is not None and pd.notna(o["oee"]) else None,
            "Availability %": (o["availability"] * 100) if o is not None and pd.notna(o["availability"]) else None,
            "Process values": " · ".join(proc),
        })
    df = pd.DataFrame(rows)
    for col in ("Speed %", "Good", "Scrap %", "OEE %", "Availability %"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    st.markdown("#### Live status")
    st.dataframe(df, hide_index=True, width="stretch", column_config={
        "Speed %": st.column_config.ProgressColumn("Speed vs target", format="%.0f%%", min_value=0, max_value=110),
        "OEE %": st.column_config.ProgressColumn("OEE (shift)", format="%.0f%%", min_value=0, max_value=100),
        "Availability %": st.column_config.NumberColumn(format="%.0f%%"),
        "Scrap %": st.column_config.NumberColumn(format="%.1f%%"),
        "Good": st.column_config.NumberColumn(format="%d"),
    })
    st.caption("OEE per unit for the current shift, using the plant's OEE model (see the OEE board). ⚠ marks a value outside its limits.")


def _trends():
    e = ui.engine()
    c = ui.cfg()
    st.markdown("#### Signal trends")
    defs = db.read_df(e, db.signal_defs, active_only=True)
    num_defs = defs[defs["kind"].isin(["rate", "process", "counter"])]
    lines = c["lines"].set_index("id")["name"]
    st_names = {int(s.id): f"{lines.get(s.line_id)} · {s.name}" for s in c["stations"].itertuples()}
    a, b, d = st.columns([2, 2, 1])
    station = a.selectbox("Station", list(st_names), format_func=st_names.get, key="tr_station")
    keys = num_defs[num_defs["station_id"] == station]
    opts = list(keys["key"])
    labels = dict(zip(keys["key"], keys["label"]))
    sel = b.multiselect("Signals", opts, default=[k for k in opts if k in ("speed", "temperature", "pressure", "vibration")][:2],
                        format_func=lambda k: labels.get(k, k), key=f"tr_keys_{station}")
    window = d.selectbox("Window", ["30 min", "2 h", "8 h", "24 h"], index=1, key="tr_win")
    mins = {"30 min": 30, "2 h": 120, "8 h": 480, "24 h": 1440}[window]
    if not sel:
        st.caption("Pick one or more signals.")
        return
    h = signals.history_df(e, db.utcnow() - pd.Timedelta(minutes=mins), [station], sel)
    if h.empty:
        st.caption("No history for this window yet.")
        return
    zone = ui.zone()
    h["t"] = h["ts"].dt.tz_localize(logic.UTC).dt.tz_convert(zone)
    for i, k in enumerate(sel):
        d_ = keys[keys["key"] == k].iloc[0]
        hk = h[h["key"] == k]
        fig = go.Figure(go.Scatter(x=hk["t"], y=hk["num"], mode="lines", line=dict(width=2, color=COLORS[i % len(COLORS)]), name=d_["label"]))
        for lim, name in ((d_["lo"], "min"), (d_["hi"], "max"), (d_["target"], "target")):
            if lim is not None and not pd.isna(lim) and not (k == "scrap" and name == "target"):
                fig.add_hline(y=lim, line_dash="dot", line_color="#dc2626" if name != "target" else "#16a34a",
                              annotation_text=name, annotation_position="top left")
        fig.update_xaxes(tickformat="%H:%M" if mins <= 480 else "%d %b %H:%M")
        fig.update_layout(height=220, margin=dict(l=4, r=4, t=28, b=4), title=dict(text=f"{d_['label']} ({d_['unit'] or ''})", font=dict(size=13)),
                          showlegend=False)
        st.plotly_chart(fig, width="stretch", config={"displaylogo": False}, key=f"tr_{station}_{k}")
