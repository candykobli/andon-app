"""Andon analytics: downtime, response times, Pareto of causes, hotspots, shifts and auto vs manual calls."""
from __future__ import annotations

from datetime import datetime, time, timedelta

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from andon import db, logic, ui

PALETTE = ["#2563eb", "#9333ea", "#0891b2", "#64748b", "#e0861a", "#17907c", "#c2457a", "#5c8a25"]


def _fig(fig, h=330, legend=True):
    fig.update_layout(height=h, margin=dict(l=6, r=6, t=10, b=6), showlegend=legend,
                      legend=dict(orientation="h", y=-0.18, x=0, title=None))
    fig.update_xaxes(title=None)
    fig.update_yaxes(title=None)
    return fig


def render():
    ui.require("staff")
    ui.need("analytics", "Analytics")
    ui.css()
    c = ui.cfg()
    e = ui.engine()
    zone, shifts = ui.zone(), ui.shifts()
    st.markdown("## Analytics")
    today = ui.now_local().date()
    f = st.columns([1.6, 1.4, 1.4, 1.4, 1.2])
    rng = f[0].date_input("Period", (today - timedelta(days=13), today), max_value=today, format="DD/MM/YYYY", key="an_rng")
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        st.stop()
    lines = f[1].multiselect("Lines", list(c["lines_all"]["id"]), placeholder="All lines", key="an_lines",
                             format_func=lambda i: c["lines_all"].set_index("id").loc[i, "name"])
    types = f[2].multiselect("Call types", list(c["call_types_all"]["id"]), placeholder="All types", key="an_types",
                             format_func=lambda i: c["call_types_all"].set_index("id").loc[i, "name"])
    sh = f[3].multiselect("Shifts", [s[0] for s in shifts], placeholder="All shifts", key="an_shifts")
    src = f[4].selectbox("Raised by", ["All", "People", "Machine rules"], key="an_src")

    start = logic.local_to_utc(datetime.combine(rng[0], time(0)), zone)
    end = logic.local_to_utc(datetime.combine(rng[1] + timedelta(days=1), time(0)), zone)
    now = db.utcnow()
    h = logic.enrich_history(db.calls_between(e, start, end), c, zone, shifts, now)
    if not h.empty:
        if lines:
            h = h[h["line_id"].isin(lines)]
        if types:
            h = h[h["call_type_id"].isin(types)]
        if sh:
            h = h[h["shift"].isin(sh)]
        if src != "All":
            h = h[h["source"] == ("auto" if src == "Machine rules" else "manual")]
    if h.empty:
        st.info("No calls in this selection.")
        return

    # previous period for comparison
    span = end - start
    prev = logic.enrich_history(db.calls_between(e, start - span, start), c, zone, shifts, now)
    if not prev.empty:
        for col, vals in (("line_id", lines), ("call_type_id", types)):
            if vals:
                prev = prev[prev[col].isin(vals)]
        if sh:
            prev = prev[prev["shift"].isin(sh)]
        if src != "All":
            prev = prev[prev["source"] == ("auto" if src == "Machine rules" else "manual")]
    k, kp = logic.kpis(h), logic.kpis(prev)

    def delta(a, b, fmt, inverse=True):
        if a is None or b in (None, 0):
            return None
        return fmt(a - b)

    m = st.columns(6)
    m[0].metric("Calls", f"{k['calls']:,}", delta(k["calls"], kp["calls"], lambda d: f"{d:+,.0f}"), delta_color="inverse", border=True,
                help="Compared with the previous period of the same length")
    m[1].metric("Line stops", f"{k['stops']:,}", delta(k["stops"], kp["stops"], lambda d: f"{d:+,.0f}"), delta_color="inverse", border=True)
    m[2].metric("Downtime", logic.fmt_min(k["downtime"]), delta(k["downtime"], kp["downtime"], lambda d: f"{d:+,.0f} min"),
                delta_color="inverse", border=True, help="Time from line stop to resolution")
    m[3].metric("Avg response (MTTA)", logic.fmt_min(k["mtta"]), delta(k["mtta"], kp["mtta"], lambda d: f"{d:+.1f} min"),
                delta_color="inverse", border=True, help="Mean time from call to acknowledgement")
    m[4].metric("Avg to resolve (MTTR)", logic.fmt_min(k["mttr"]), delta(k["mttr"], kp["mttr"], lambda d: f"{d:+.1f} min"),
                delta_color="inverse", border=True, help="Mean time from call to resolution")
    m[5].metric("Escalated", f"{k['escalated'] * 100:.0f}%" if k["escalated"] is not None else "–",
                delta(k["escalated"], kp["escalated"], lambda d: f"{d * 100:+.0f} pts"), delta_color="inverse", border=True,
                help="Calls not acknowledged before the level-2 escalation time")

    colors = {r.name: (r.color or PALETTE[i % len(PALETTE)]) for i, r in enumerate(c["call_types_all"].itertuples())}

    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Calls per day by type**")
        daily = h.groupby([h["prod_date"].dt.date, "type"]).size().reset_index(name="Calls")
        fig = px.bar(daily, x="prod_date", y="Calls", color="type", color_discrete_map=colors)
        st.plotly_chart(_fig(fig), width="stretch", key="an_daily")
    with b.container(border=True):
        st.markdown("**Downtime Pareto by cause**")
        stops = h[h["line_stop"]]
        if stops.empty:
            st.caption("No line stops in this selection.")
        else:
            p = stops.groupby(["reason", "type"])["downtime_min"].sum().reset_index().sort_values("downtime_min", ascending=False).head(12)
            p["cum"] = p["downtime_min"].cumsum() / stops["downtime_min"].sum() * 100
            p["label"] = p["reason"].map(lambda s: s if len(s) <= 18 else s[:17] + "…")
            fig = go.Figure()
            fig.add_bar(x=p["label"], customdata=p["reason"], y=p["downtime_min"], marker_color=[colors.get(t, "#64748b") for t in p["type"]],
                        name="Downtime (min)", hovertemplate="%{customdata}: %{y:.0f} min<extra></extra>")
            fig.add_scatter(x=p["label"], y=p["cum"], yaxis="y2", mode="lines+markers", line=dict(color="#dc2626"),
                            name="Cumulative %", hovertemplate="%{y:.0f}%<extra></extra>")
            fig.update_layout(yaxis2=dict(overlaying="y", side="right", range=[0, 105], ticksuffix="%", showgrid=False,
                                          tickvals=[0, 25, 50, 75, 100]))
            fig.update_xaxes(tickangle=-35)
            st.plotly_chart(_fig(fig), width="stretch", key="an_pareto")
            top = p.iloc[0]
            st.caption(f"Biggest cause: **{top['reason']}** ({top['type']}) with {logic.fmt_min(top['downtime_min'])}, "
                       f"{top['downtime_min'] / stops['downtime_min'].sum():.0%} of all downtime.")

    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**Hotspots: downtime by station**")
        hm = h.groupby(["line", "station"])["downtime_min"].sum().reset_index()
        piv = hm.pivot(index="line", columns="station", values="downtime_min").fillna(0)
        fig = px.imshow(piv.round(0), color_continuous_scale=["#f1f5f9", "#fbbf24", "#dc2626"], aspect="auto", text_auto=True)
        fig.update_layout(coloraxis_showscale=False)
        st.plotly_chart(_fig(fig, legend=False), width="stretch", key="an_heat")
        worst = hm.sort_values("downtime_min", ascending=False).iloc[0]
        st.caption(f"Most downtime: **{worst['line']} · {worst['station']}** ({logic.fmt_min(worst['downtime_min'])}).")
    with b.container(border=True):
        st.markdown("**Response and resolution time by call type**")
        t = h.groupby("type")[["mtta_min", "mttr_min"]].mean().reset_index().melt(id_vars="type", var_name="Measure", value_name="Minutes")
        t["Measure"] = t["Measure"].map({"mtta_min": "To acknowledge", "mttr_min": "To resolve"})
        fig = px.bar(t, x="type", y="Minutes", color="Measure", barmode="group", color_discrete_sequence=["#f59e0b", "#2563eb"])
        st.plotly_chart(_fig(fig), width="stretch", key="an_resp")

    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("**By shift**")
        s = h.groupby("shift").agg(Calls=("id", "size"), Downtime=("downtime_min", "sum"), MTTA=("mtta_min", "mean")).reset_index()
        order = [x[0] for x in shifts]
        s["shift"] = pd.Categorical(s["shift"], order, ordered=True)
        s = s.sort_values("shift")
        fig = go.Figure()
        fig.add_bar(x=s["shift"], y=s["Downtime"], name="Downtime (min)", marker_color="#dc2626")
        fig.add_scatter(x=s["shift"], y=s["MTTA"], name="Avg response (min)", yaxis="y2", mode="lines+markers", line=dict(color="#2563eb"))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right", showgrid=False))
        st.plotly_chart(_fig(fig), width="stretch", key="an_shift")
        slow = s.sort_values("MTTA", ascending=False).iloc[0]
        st.caption(f"Slowest response: **{slow['shift']}** shift, {logic.fmt_min(slow['MTTA'])} on average.")
    with b.container(border=True):
        st.markdown("**Calls by hour of day**")
        hr = h.groupby(["hour", "type"]).size().reset_index(name="Calls")
        fig = px.bar(hr, x="hour", y="Calls", color="type", color_discrete_map=colors)
        fig.update_xaxes(dtick=2)
        st.plotly_chart(_fig(fig), width="stretch", key="an_hour")

    with st.container(border=True):
        st.markdown("**Top stops** · by exact problem and location (planned stops and microstops excluded)")
        from views.oee_board import top_stops
        ts = top_stops(e, c, lines or list(c["lines_all"]["id"]), start, end, n=15)
        if ts is None or ts.empty:
            st.caption("No stops in this selection.")
        else:
            a1, b1 = st.columns([1.1, 1])
            byev = ts.groupby("event").agg(minutes=("minutes", "sum"), stops=("stops", "sum")).reset_index().sort_values("minutes")
            fig = go.Figure(go.Bar(y=byev["event"], x=byev["minutes"], orientation="h", marker_color="#dc2626",
                                   customdata=byev["stops"], hovertemplate="%{y}: %{x:.0f} min in %{customdata} stops<extra></extra>"))
            a1.plotly_chart(_fig(fig, h=max(260, 26 * len(byev) + 40), legend=False), width="stretch", key="an_topstops")
            b1.dataframe(ts, hide_index=True, width="stretch", column_config={
                "event": "Problem", "where": "Where", "stops": st.column_config.NumberColumn("Stops", format="%d"),
                "minutes": st.column_config.NumberColumn("Total (min)", format="%.0f"),
                "longest": st.column_config.NumberColumn("Longest (min)", format="%.0f"),
                "ongoing": st.column_config.CheckboxColumn("Ongoing")})

    with st.container(border=True):
        st.markdown("**Longest line stops**")
        cols = ["raised_local", "line", "station", "type", "reason", "downtime_min", "mtta_min", "source", "resolved_by", "note"]
        t = h[h["line_stop"]].copy()
        t["raised_local"] = t["raised_at"].dt.tz_localize(logic.UTC).dt.tz_convert(zone).dt.strftime("%d %b %H:%M")
        t = t.sort_values("downtime_min", ascending=False).head(15)[cols]
        st.dataframe(t, hide_index=True, width="stretch", column_config={
            "raised_local": "Raised", "line": "Line", "station": "Station", "type": "Type", "reason": "Cause",
            "downtime_min": st.column_config.NumberColumn("Downtime (min)", format="%.0f"),
            "mtta_min": st.column_config.NumberColumn("Response (min)", format="%.1f"),
            "source": "Raised by", "resolved_by": "Resolved by", "note": "Note"})
    out = h.drop(columns=["prod_date"]).copy()
    out["raised_local"] = out["raised_at"].dt.tz_localize(logic.UTC).dt.tz_convert(zone).dt.tz_localize(None)
    st.download_button("Download all calls in this selection (CSV)", out.to_csv(index=False).encode("utf-8-sig"),
                       f"andon_calls_{rng[0]}_{rng[1]}.csv", "text/csv", icon=":material/download:")
