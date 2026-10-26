"""OEE board with three views by access level, plus a TV mode for the shop floor.

operator   (and TV: /oee?kiosk=1&lines=1,2&key=…)  OEE vs target, A/P/Q, good output, scrap, running and last batch,
            top stops this shift, OEE per hour, latest handover note
supervisor  + machines, top stops with durations, OEE per shift, batch start/end, handover notes
full        (line manager, process engineer, viewer, admin) + OEE, TCU, OEE3, OEE2, OEE1 actual vs target per line,
            time-loss waterfall from total calendar time to valued operating time, per hour/shift/machine, export
"""
from __future__ import annotations

import html
from datetime import datetime, time, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from andon import db, logic, oee, signals, ui

GREEN, AMBER, RED, BLUE, GREY = "#009e73", "#e69f00", "#d55e00", "#2563eb", "#64748b"  # Okabe-Ito: safe for colour-blind viewers

TV_CSS = """
<style>
.oe-wrap {background:#f4f6f9; color:#0f172a; border-radius:14px; padding:16px}
.oe-head {display:flex; justify-content:space-between; align-items:baseline; flex-wrap:wrap; gap:8px; margin-bottom:12px}
.oe-head h2 {margin:0; color:#0f172a; font-size:1.6rem}
.oe-head span {color:#475569}
.oe-grid {display:grid; grid-template-columns: repeat(auto-fit, minmax(330px, 1fr)); gap:12px}
.oe-card {background:#fff; border:1px solid #e2e8f0; border-radius:12px; padding:14px; display:flex; flex-direction:column; gap:10px}
.oe-card h3 {margin:0; color:#0f172a; font-size:1.25rem}
.oe-big {display:flex; align-items:baseline; gap:12px; flex-wrap:wrap}
.oe-big b {font-size:3.2rem; line-height:1; font-variant-numeric:tabular-nums}
.oe-big small {color:#475569; font-size:.95rem}
.oe-tg {font-size:.8rem; font-weight:800; letter-spacing:.05em; text-transform:uppercase; padding:2px 8px; border-radius:999px;
        border:2px solid currentColor}
.oe-apq {display:grid; grid-template-columns: repeat(3, 1fr); gap:8px}
.oe-apq div {background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:6px 8px}
.oe-apq b {display:block; font-size:1.35rem; font-variant-numeric:tabular-nums}
.oe-apq span, .oe-row span {font-size:.72rem; color:#64748b; text-transform:uppercase; letter-spacing:.06em}
.oe-row {display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap}
.oe-row div b {display:block; font-size:1.15rem; font-variant-numeric:tabular-nums}
.oe-batch {background:#f8fafc; border:1px solid #e2e8f0; border-radius:8px; padding:8px 10px; font-size:.92rem}
.oe-batch b {color:#0f172a}
.oe-stops {font-size:.9rem; margin:0; padding-left:18px}
.oe-hours {display:flex; align-items:flex-end; gap:3px; height:70px; border-bottom:1px solid #cbd5e1}
.oe-hours i {flex:1; max-width:22px; border-radius:2px 2px 0 0; display:block; min-height:2px}
.oe-note {font-size:.88rem; color:#334155; border-left:3px solid #1d4ed8; padding-left:8px}
</style>
"""



def _pct(v, d=0):
    return "–" if v is None or pd.isna(v) else f"{v * 100:.{d}f}%"


def _color(v, t):
    if v is None or t is None or pd.isna(v):
        return GREY
    return GREEN if v >= t else (AMBER if v >= t * 0.9 else RED)


def _pattern(v, t) -> str:
    """Bar pattern: below target is striped, near target dotted, so colour is never the only signal."""
    if v is None or t is None or pd.isna(v) or v >= t:
        return ""
    return "." if v >= t * 0.9 else "/"


def _css_pattern(v, t) -> str:
    p = _pattern(v, t)
    if p == "/":
        return "background-image:repeating-linear-gradient(135deg,rgba(255,255,255,.55) 0 3px,transparent 3px 7px)"
    if p == ".":
        return "background-image:radial-gradient(rgba(255,255,255,.6) 1px,transparent 1.5px);background-size:5px 5px"
    return ""


def _tcolor(v, t):
    """Text colour for a value against target on the light TV screen (dark shades, readable on white)."""
    if v is None or t is None or pd.isna(v):
        return "#64748b"
    return "#0e7c5a" if v >= t else ("#a15c00" if v >= t * 0.9 else "#b42318")


def _tarrow(v, t) -> str:
    if v is None or t is None or pd.isna(v):
        return ""
    return "▲" if v >= t else "▼"


def _tword(v, t) -> str:
    """Words as well as colour, so the status is clear for colour-blind viewers."""
    if v is None or t is None or pd.isna(v):
        return ""
    ok = v >= t
    return f"<span class='oe-tg' style='color:{_tcolor(v, t)}'>{'▲ on target' if ok else '▼ below target'}</span>"


def _period(choice: str, zone, shifts) -> tuple[datetime, datetime, str]:
    now = db.utcnow()
    now_l = ui.now_local()
    if choice == "This shift":
        name, s, e = logic.current_shift_window(now_l, shifts)
        return s.astimezone(logic.UTC).replace(tzinfo=None), now, f"{name} shift"
    if choice == "Last shift":
        name, s, e = logic.current_shift_window(now_l, shifts)
        pn, ps, pe = logic.current_shift_window(s - timedelta(minutes=1), shifts)
        return ps.astimezone(logic.UTC).replace(tzinfo=None), pe.astimezone(logic.UTC).replace(tzinfo=None), f"{pn} shift (previous)"
    if choice == "Today":
        s = datetime.combine(now_l.date(), time(0), tzinfo=zone)
        return s.astimezone(logic.UTC).replace(tzinfo=None), now, "Today"
    if choice == "Last 24 h":
        return now - timedelta(hours=24), now, "Last 24 hours"
    if choice == "Last 7 days":
        return now - timedelta(days=7), now, "Last 7 days"
    return now - timedelta(days=14), now, "Last 14 days"


def render():
    ui.require("floor")
    kiosk = st.query_params.get("kiosk") == "1"
    view = "operator" if kiosk else ui.oee_view()
    c = ui.cfg()
    ui.css()
    lines_all = c["lines"][c["lines"]["id"].isin(ui.allowed_lines(c))]
    qp_lines = st.query_params.get("lines")
    default = list(lines_all["id"])
    if qp_lines:
        try:
            default = [int(x) for x in qp_lines.split(",") if int(x) in set(lines_all["id"])] or default
        except ValueError:
            pass
    if kiosk:
        _tv(default, "This shift")
        return
    st.markdown("## OEE board")
    a, b, d = st.columns([3, 2, 2], vertical_alignment="bottom")
    sel = a.multiselect("Lines", list(lines_all["id"]), default=default, key="oee_lines",
                        format_func=lambda i: lines_all.set_index("id").loc[i, "name"])
    period = b.selectbox("Period", ["This shift", "Last shift", "Today", "Last 24 h", "Last 7 days", "Last 14 days"], key="oee_period")
    d.caption(f"View: **{ {'operator': 'Operator', 'supervisor': 'Supervisor', 'full': 'Line manager / engineer'}[view] }**"
              + (" · sign in for more detail" if view == "operator" else ""))
    if not sel:
        st.info("Choose at least one line.")
        return
    _cards(sel, period)
    if view in ("supervisor", "full"):
        _supervisor(sel, period)
    if view == "full":
        _full(sel, period)
    st.caption(f"TV link for this selection: **/oee?kiosk=1&lines={','.join(map(str, sel))}**"
               + ("&key=…" if ui.secret("DEVICE_KEY") else "") + " · " + _definitions_hint(c))


def _definitions_hint(c) -> str:
    base = c["settings"].get("oee_headline_base", "oee2")
    return ("OEE = availability × performance × quality on planned production time (same as OEE2)" if base != "oee1"
            else "OEE = availability × performance × quality on line-controllable time (same as OEE1)")


def _line_data(e, c, line_id: int, start, end):
    stn = c["stations"][c["stations"]["line_id"] == line_id]
    outs = oee.output_stations(stn)
    df = oee.load(e, start, end + timedelta(hours=1), list(stn["id"]))
    return stn, outs, df


def _batches(e, line_id):
    with e.connect() as cx:
        from sqlalchemy import select
        rows = pd.read_sql(select(db.batches).where(db.batches.c.line_id == line_id).order_by(db.batches.c.started_at.desc()).limit(2),
                           cx, parse_dates=["started_at", "ended_at"])
    running = rows[rows["ended_at"].isna()].head(1)
    last = rows[rows["ended_at"].notna()].head(1)
    return (running.iloc[0].to_dict() if not running.empty else None), (last.iloc[0].to_dict() if not last.empty else None)


def top_stops(e, c, line_ids, start, end, n=10, lang="en") -> pd.DataFrame:
    return oee.top_stops(e, c, line_ids, start, end, n, lang)


def _hour_series(df_out, start, end, base, zone):
    if df_out.empty:
        return pd.DataFrame(columns=["hour", "oee"])
    rows = []
    daily = start is not None and end is not None and (end - start) > timedelta(hours=26)  # longer periods: one bar per day instead of overlapping hours
    local = df_out["hour"].dt.tz_localize(logic.UTC).dt.tz_convert(zone)
    key = local.dt.strftime("%Y-%m-%d") if daily else df_out["hour"]
    for k, g in df_out.groupby(key):
        m = oee.metrics(g, base)
        if m["oee"] is not None:
            rows.append({"hour": k, "oee": m["oee"], "good": m["good"]})
    out = pd.DataFrame(rows, columns=["hour", "oee", "good"])
    if out.empty:
        return out.assign(label=[])
    out["label"] = (pd.to_datetime(out["hour"]).dt.strftime("%a %d") if daily
                    else out["hour"].dt.tz_localize(logic.UTC).dt.tz_convert(zone).dt.strftime("%H:00"))
    out.attrs["daily"] = daily
    return out


# ------------------------------------------------------------------ operator cards (also the TV)
def _cards(sel, period):
    e, c = ui.engine(), ui.cfg()
    zone, shifts = ui.zone(), ui.shifts()
    base = c["settings"].get("oee_headline_base", "oee2")
    start, end, label = _period(period, zone, shifts)
    tg = oee.targets(e)
    cols = st.columns(min(3, len(sel)))
    for i, lid in enumerate(sel):
        ln = c["lines"].set_index("id").loc[lid]
        stn, outs, df = _line_data(e, c, lid, start, end)
        m = oee.metrics(df[df["station_id"].isin(outs)], base)
        t_oee = oee.target_for(tg, lid, "oee")
        with cols[i % len(cols)].container(border=True):
            st.markdown(f"### {ln['name']} <span style='font-size:.8rem;opacity:.7'>{html.escape(str(ln['area']))} · {label}</span>",
                        unsafe_allow_html=True)
            col = _color(m["oee"], t_oee)
            st.markdown(f"<div style='display:flex;align-items:baseline;gap:12px'><span style='font-size:2.8rem;font-weight:800;"
                        f"color:{col};font-variant-numeric:tabular-nums'>{_pct(m['oee'])}</span>"
                        f"<span style='opacity:.7'>OEE · target {_pct(t_oee)}</span></div>", unsafe_allow_html=True)
            a, p, q = st.columns(3)
            for colw, key, name in ((a, "availability", "Availability"), (p, "performance", "Performance"), (q, "quality", "Quality")):
                t = oee.target_for(tg, lid, key)
                v = m[key]
                colw.metric(name, _pct(v), f"{(v - t) * 100:+.1f} pts" if v is not None and t else None, border=True)
            good, scrap = m["good"], m["scrap"]
            g1, g2 = st.columns(2)
            g1.metric("Good output", f"{good:,.0f}", border=True)
            g2.metric("Scrap", f"{scrap:,.0f}", f"{scrap / (good + scrap) * 100:.1f}%" if good + scrap else None, delta_color="off", border=True)
            run, last = _batches(e, lid)
            if run:
                prog = f" · {good / run['planned_qty']:.0%} of {run['planned_qty']:,.0f} planned" if pd.notna(run.get("planned_qty")) and run.get("planned_qty") and period == "This shift" else ""
                st.markdown(f"**Running batch** {html.escape(run['batch_no'])} · {html.escape(run['product'])}  \n"
                            f"Destination: **{html.escape(str(run.get('destination') or '–'))}** · since "
                            f"{pd.Timestamp(run['started_at']).tz_localize(logic.UTC).tz_convert(zone):%H:%M}{prog}")
            else:
                st.markdown("**Running batch:** none (changeover or not scheduled)")
            if last:
                st.caption(f"Last batch {last['batch_no']} · {last['product']} → {last.get('destination') or '–'} · ended "
                           f"{pd.Timestamp(last['ended_at']).tz_localize(logic.UTC).tz_convert(zone):%d %b %H:%M}")
            ts = top_stops(e, c, [lid], start, end, n=3)
            if ts is not None and not ts.empty:
                st.markdown("**Top stops**  \n" + "  \n".join(f"{r.event} · {r.where.split(' · ')[-1]} · {r.minutes:.0f} min ({r.stops}×)"
                                                              for r in ts.itertuples()))
            hs = _hour_series(df[df["station_id"].isin(outs)], start, end, base, zone)
            if len(hs) > 1:
                fig = go.Figure(go.Bar(x=hs["label"], y=hs["oee"] * 100, marker_color=[_color(v, t_oee) for v in hs["oee"]],
                                       marker_pattern_shape=[_pattern(v, t_oee) for v in hs["oee"]], marker_pattern_fgcolor="#ffffff",
                                       hovertemplate="%{x}: %{y:.0f}%<extra></extra>"))
                if t_oee:
                    fig.add_hline(y=t_oee * 100, line_dash="dot", line_color=GREY)
                fig.update_layout(height=150, margin=dict(l=4, r=4, t=4, b=4), yaxis=dict(range=[0, 100], ticksuffix="%"), showlegend=False)
                st.caption("OEE per day" if hs.attrs.get("daily") else "OEE per hour")
                st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key=f"oeh_{lid}_{period}")
            note = _last_note(e, lid)
            if note:
                st.info(f"**Handover** · {note['author']}, {pd.Timestamp(note['ts']).tz_localize(logic.UTC).tz_convert(zone):%d %b %H:%M}: "
                        f"{note['note']}", icon=":material/sticky_note_2:")


def _last_note(e, lid):
    from sqlalchemy import select
    with e.connect() as cx:
        r = cx.execute(select(db.handover).where(db.handover.c.line_id == int(lid)).order_by(db.handover.c.ts.desc()).limit(1)).mappings().first()
    return dict(r) if r else None


@st.fragment(run_every="30s")
def _tv(sel, period):
    e, c = ui.engine(), ui.cfg()
    signals.simulate_if_due(e)
    zone, shifts = ui.zone(), ui.shifts()
    base = c["settings"].get("oee_headline_base", "oee2")
    start, end, label = _period(period, zone, shifts)
    tg = oee.targets(e)
    now_l = ui.now_local()
    parts = [TV_CSS, "<div class='oe-wrap'><div class='oe-head'>",
             f"<h2>{html.escape(c['settings'].get('plant_name', 'Plant'))} · OEE</h2><span>{label} · {now_l:%a %d %b %H:%M}</span></div>",
             "<div class='oe-grid'>"]
    for lid in sel:
        ln = c["lines"].set_index("id").loc[lid]
        stn, outs, df = _line_data(e, c, lid, start, end)
        m = oee.metrics(df[df["station_id"].isin(outs)], base)
        t_oee = oee.target_for(tg, lid, "oee")
        parts.append(f"<div class='oe-card'><h3>{html.escape(ln['name'])} <small style='color:#64748b;font-size:.8rem'>{html.escape(str(ln['area']))}</small></h3>")
        parts.append(f"<div class='oe-big'><b style='color:{_tcolor(m['oee'], t_oee)}'>{_pct(m['oee'])}</b>"
                     f"<small>OEE · target {_pct(t_oee)}</small>{_tword(m['oee'], t_oee)}</div>")
        parts.append("<div class='oe-apq'>" + "".join(
            f"<div><span>{n}</span><b style='color:{_tcolor(m[k], oee.target_for(tg, lid, k))}'>{_pct(m[k])} "
            f"{_tarrow(m[k], oee.target_for(tg, lid, k))}</b></div>"
            for k, n in (("availability", "Availability"), ("performance", "Performance"), ("quality", "Quality"))) + "</div>")
        tot = m["good"] + m["scrap"]
        scrap_pct = " · {:.1f}%".format(m["scrap"] / tot * 100) if tot else ""
        parts.append(f"<div class='oe-row'><div><span>Good output</span><b>{m['good']:,.0f}</b></div>"
                     f"<div><span>Scrap</span><b>{m['scrap']:,.0f}{scrap_pct}</b></div></div>")
        run, last = _batches(e, lid)
        if run:
            parts.append(f"<div class='oe-batch'>Batch <b>{html.escape(run['batch_no'])}</b> · {html.escape(run['product'])}<br>"
                         f"Destination <b>{html.escape(str(run.get('destination') or '–'))}</b></div>")
        else:
            parts.append("<div class='oe-batch'>No batch running (changeover or not scheduled)</div>")
        ts = top_stops(e, c, [lid], start, end, n=3)
        if ts is not None and not ts.empty:
            parts.append("<ol class='oe-stops'>" + "".join(f"<li>{html.escape(r.event)} · {html.escape(r.where.split(' · ')[-1])} · {r.minutes:.0f} min</li>"
                                                          for r in ts.itertuples()) + "</ol>")
        hs = _hour_series(df[df["station_id"].isin(outs)], start, end, base, zone)
        if len(hs):
            parts.append("<div class='oe-hours'>" + "".join(
                f"<i title='{r.label} {_pct(r.oee)}' style='height:{max(2, (r.oee or 0) * 100):.0f}%;background:{_color(r.oee, t_oee)};{_css_pattern(r.oee, t_oee)}'></i>"
                for r in hs.itertuples()) + "</div>")
        note = _last_note(e, lid)
        if note and (pd.Timestamp(db.utcnow()) - pd.Timestamp(note["ts"])) < pd.Timedelta(hours=24):
            parts.append(f"<div class='oe-note'>{html.escape(note['note'])}</div>")
        parts.append("</div>")
    parts.append("</div></div>")
    parts.append("<style>header, [data-testid='stHeader'], [data-testid='stToolbar'] {display:none !important}"
                 ".block-container {padding:12px !important; max-width:100% !important}</style>")
    st.html("".join(parts))


# ------------------------------------------------------------------ supervisor additions
def _supervisor(sel, period):
    e, c = ui.engine(), ui.cfg()
    zone, shifts = ui.zone(), ui.shifts()
    base = c["settings"].get("oee_headline_base", "oee2")
    start, end, label = _period(period, zone, shifts)
    st.divider()
    a, b = st.columns([1.2, 1])
    with a.container(border=True):
        st.markdown(f"**Machines · {label}**")
        sm = oee.station_metrics(e, start, end + timedelta(hours=1), base)
        stn = c["stations"][c["stations"]["line_id"].isin(sel)].merge(
            c["lines"][["id", "name"]].rename(columns={"id": "line_id", "name": "line"}), on="line_id")
        snap = signals.station_snapshot(signals.latest_df(e), db.read_df(e, db.signal_defs, active_only=True), db.utcnow())
        t = stn.merge(sm, left_on="id", right_on="station_id", how="left")
        t["state"] = t["id"].map(lambda i: (snap.get(int(i), {}).get("state", {}) or {}).get("value"))
        view = t[["line", "name", "process", "state", "oee", "availability", "performance", "quality", "good", "scrap"]].copy()
        for k in ("oee", "availability", "performance", "quality"):
            view[k] = pd.to_numeric(view[k], errors="coerce") * 100
        for k in ("good", "scrap"):
            view[k] = pd.to_numeric(view[k], errors="coerce")
        # text columns so units with no production show a dash instead of "None"
        for k, f in (("availability", "{:.0f}%"), ("performance", "{:.0f}%"), ("quality", "{:.1f}%"), ("good", "{:,.0f}"), ("scrap", "{:,.0f}")):
            view[k] = view[k].map(lambda v, f=f: f.format(v) if pd.notna(v) and (k not in ("good", "scrap") or v) else "–")
        view["state"] = view["state"].replace({"Off": "Not scheduled"}).fillna("–")
        st.dataframe(view, hide_index=True, width="stretch", column_config={
            "line": "Line", "name": "Unit", "process": "Process", "state": "State",
            "oee": st.column_config.ProgressColumn("OEE", format="%.0f%%", min_value=0, max_value=100),
            "availability": "A", "performance": "P", "quality": "Q", "good": "Good", "scrap": "Scrap"})
    with b.container(border=True):
        st.markdown(f"**Top stops · {label}**")
        ts = top_stops(e, c, sel, start, end, n=10)
        if ts is None or ts.empty:
            st.caption("No stops longer than a microstop in this period.")
        else:
            st.dataframe(ts, hide_index=True, width="stretch", column_config={
                "event": "Problem", "where": "Where", "stops": st.column_config.NumberColumn("Stops", format="%d"),
                "minutes": st.column_config.NumberColumn("Total (min)", format="%.0f"),
                "longest": st.column_config.NumberColumn("Longest (min)", format="%.0f"),
                "ongoing": st.column_config.CheckboxColumn("Ongoing")})
    a, b = st.columns([1.2, 1])
    with a.container(border=True):
        st.markdown("**OEE per shift · last 7 days**")
        df = oee.load(e, db.utcnow() - timedelta(days=7), db.utcnow() + timedelta(hours=1))
        stn_all = c["stations"]
        rows = []
        for lid in sel:
            outs = oee.output_stations(stn_all[stn_all["line_id"] == lid])
            d = df[df["station_id"].isin(outs)].copy()
            if d.empty:
                continue
            local = d["hour"].dt.tz_localize(logic.UTC).dt.tz_convert(zone)
            sd = [logic.shift_of(t.to_pydatetime(), shifts) for t in local]
            d["shift_key"] = [f"{dd:%d %b} {s}" for s, dd in sd]
            d["sort"] = [pd.Timestamp(dd) + pd.Timedelta(hours=[x[0] for x in shifts].index(s) if s in [x[0] for x in shifts] else 0) for s, dd in sd]
            for (key, srt), g in d.groupby(["shift_key", "sort"]):
                m = oee.metrics(g, base)
                if m["oee"] is not None and m["times"]["ppt"] > 1800:
                    rows.append({"line": c["lines"].set_index("id").loc[lid, "name"], "shift": key, "sort": srt, "oee": m["oee"] * 100})
        if rows:
            r = pd.DataFrame(rows).sort_values("sort")
            fig = go.Figure()
            for i, (ln, g) in enumerate(r.groupby("line")):
                fig.add_scatter(x=g["shift"], y=g["oee"], mode="lines+markers", name=ln,
                                line=dict(color=[BLUE, "#e0861a", "#17907c", "#c2457a"][i % 4], width=2))
            fig.update_layout(height=280, margin=dict(l=4, r=4, t=4, b=4), yaxis=dict(ticksuffix="%", range=[0, 100]),
                              legend=dict(orientation="h", y=-0.3),
                              xaxis=dict(tickangle=-45, categoryorder="array", categoryarray=list(dict.fromkeys(r["shift"]))))
            st.plotly_chart(fig, width="stretch", key="oee_shift_chart")
        else:
            st.caption("Not enough history yet.")
    with b.container(border=True):
        st.markdown("**Batch and handover**")
        lid = st.selectbox("Line", sel, format_func=lambda i: c["lines"].set_index("id").loc[i, "name"], key="sup_line")
        run, _ = _batches(e, lid)
        u = ui.current_user()
        if ui.can("batch"):
            if run:
                st.caption(f"Running: {run['batch_no']} · {run['product']} ({run['source']})")
                if st.button("End running batch", key=f"end_b_{lid}"):
                    from sqlalchemy import update
                    with e.begin() as cx:
                        cx.execute(update(db.batches).where(db.batches.c.id == int(run["id"])).values(ended_at=db.utcnow()))
                        db.audit(cx, u["name"], "batch_ended", f"batch {run['batch_no']}", ui.client_id())
                    st.rerun()
            with st.form(f"batch_{lid}", clear_on_submit=True, border=False):
                x1, x2 = st.columns(2)
                prod = x1.text_input("Product")
                bno = x2.text_input("Batch number")
                x1, x2 = st.columns(2)
                dest = x1.text_input("Destination (market)")
                qty = x2.number_input("Planned quantity", min_value=0, step=100)
                if st.form_submit_button("Start batch") and prod and bno:
                    from sqlalchemy import insert, update
                    now = db.utcnow()
                    with e.begin() as cx:
                        cx.execute(update(db.batches).where(db.batches.c.line_id == int(lid), db.batches.c.ended_at.is_(None)).values(ended_at=now))
                        cx.execute(insert(db.batches).values(line_id=int(lid), product=prod[:120], batch_no=bno[:60], destination=dest[:80] or None,
                                                             planned_qty=float(qty) or None, started_at=now, source="manual", started_by=u["name"]))
                        db.audit(cx, u["name"], "batch_started", f"batch {bno} on line {lid}", ui.client_id())
                    st.toast(f"Batch {bno} started")
                    st.rerun()
            st.caption("When MES or ERP sends batches through the connector, they appear here automatically.")
        if ui.can("handover"):
            with st.form(f"handover_{lid}", clear_on_submit=True, border=False):
                note = st.text_area("Handover note for the next shift", height=80)
                if st.form_submit_button("Save note") and note.strip():
                    from sqlalchemy import insert
                    name, _, _ = logic.current_shift_window(ui.now_local(), shifts)
                    with e.begin() as cx:
                        cx.execute(insert(db.handover).values(line_id=int(lid), ts=db.utcnow(), shift=name, author=u["name"], note=note.strip()[:2000]))
                    st.toast("Note saved. It shows on the OEE screen for the next shift.")
                    st.rerun()
    _report_panel(sel)


def _report_panel(sel):
    """Shift report as a PDF (supervisors and up)."""
    from andon import report
    e, c = ui.engine(), ui.cfg()
    shifts = ui.shifts()
    names = [s[0] for s in shifts]
    # default: the last shift that has finished
    cur, cur_start, _ = logic.current_shift_window(ui.now_local(), shifts)
    last_name, last_date = logic.shift_of(cur_start - timedelta(minutes=1), shifts)
    with st.container(border=True):
        st.markdown("**Shift report (PDF)**: OEE against target, where the time went, OEE per hour, top stops, calls, "
                    "batches and handover notes, one page per line.")
        a, b, cc, d = st.columns([2.2, 1.2, 1.2, 1.2], vertical_alignment="bottom")
        lmap = c["lines"].set_index("id")["name"].to_dict()
        lines = a.multiselect("Lines", list(lmap), default=[i for i in sel if i in lmap], format_func=lmap.get, key="rep_lines")
        day = b.date_input("Production date", value=last_date, key="rep_date", format="DD/MM/YYYY")
        shift = cc.selectbox("Shift", names, index=names.index(last_name) if last_name in names else 0, key="rep_shift")
        if d.button("Create report", width="stretch", disabled=not lines):
            u = ui.current_user()
            with st.spinner("Building the PDF…"):
                pdf, fname = report.shift_report(e, lines, shift, day, author=u["name"] if u else "")
            st.session_state["rep_pdf"] = (pdf, fname)
            db.audit(e, u["name"] if u else "unknown", "report_created", fname, ui.client_id())
        if st.session_state.get("rep_pdf"):
            pdf, fname = st.session_state["rep_pdf"]
            st.download_button(f"Download {fname}", pdf, file_name=fname, mime="application/pdf", icon=":material/download:")


# ------------------------------------------------------------------ full view (line manager, engineer, viewer, admin)
def _full(sel, period):
    e, c = ui.engine(), ui.cfg()
    zone, shifts = ui.zone(), ui.shifts()
    base = c["settings"].get("oee_headline_base", "oee2")
    start, end, label = _period(period, zone, shifts)
    tg = oee.targets(e)
    st.divider()
    st.markdown(f"#### Loss analysis · {label}")
    rows = []
    stn = c["stations"]
    dfs = {}
    for lid in sel:
        outs = oee.output_stations(stn[stn["line_id"] == lid])
        df = oee.load(e, start, end + timedelta(hours=1), outs)
        dfs[lid] = df
        m = oee.metrics(df, base)
        row = {"Line": c["lines"].set_index("id").loc[lid, "name"]}
        for k in ["oee", "tcu", "oee3", "oee2", "oee1", "availability", "performance", "quality"]:
            row[oee.METRIC_LABEL[k]] = m[k]
            row[oee.METRIC_LABEL[k] + " target"] = oee.target_for(tg, lid, k)
        row["Good"] = m["good"]
        rows.append(row)
    kp = pd.DataFrame(rows)

    def style(v, t):
        if v is None or pd.isna(v):
            return "–"
        arrow = "" if t is None else (" ▲" if v >= t else " ▼")
        return f"{v * 100:.1f}%{arrow}"
    show = pd.DataFrame({"Line": kp["Line"]})
    for k in ["OEE", "TCU", "OEE3", "OEE2", "OEE1", "Availability", "Performance", "Quality"]:
        show[k] = [style(v, t) for v, t in zip(kp[k], kp[k + " target"])]
        show[k + " target"] = [f"{t * 100:.0f}%" if t else "–" for t in kp[k + " target"]]
    show["Good output"] = kp["Good"].round(0)
    with st.container(border=True):
        st.markdown("**Actual vs target per line** (▲ at or above target, ▼ below)")
        st.dataframe(show, hide_index=True, width="stretch")
        st.caption("TCU = valued ÷ calendar time · OEE3 = valued ÷ scheduled time · OEE2 = valued ÷ planned production time · "
                   "OEE1 = valued ÷ line-controllable time. Which stops are planned or external is set per event in Configure → Event catalogue.")
    a, b = st.columns([1.3, 1])
    with a.container(border=True):
        lid = st.selectbox("Waterfall for", ["All selected"] + sel, key="wf_line",
                           format_func=lambda i: i if i == "All selected" else c["lines"].set_index("id").loc[i, "name"])
        df = pd.concat(dfs.values()) if lid == "All selected" else dfs[lid]
        m = oee.metrics(df, base)
        t = m.get("times") or {}
        if t:
            labels, vals, measures = [], [], []
            for name, key, kind in oee.WATERFALL:
                labels.append(name)
                vals.append((t.get(key, 0) / 3600) * (-1 if kind == "loss" else 1))
                measures.append("absolute" if kind == "total" else "relative")
            fig = go.Figure(go.Waterfall(x=labels, y=vals, measure=measures, textposition="outside",
                                         text=[f"{abs(v):.0f} h" for v in vals],
                                         decreasing=dict(marker=dict(color=RED)), totals=dict(marker=dict(color=BLUE)),
                                         increasing=dict(marker=dict(color=GREEN)), connector=dict(line=dict(color="#cbd5e1"))))
            fig.update_layout(height=420, margin=dict(l=4, r=4, t=10, b=4), yaxis_title="hours", xaxis=dict(tickangle=-40), showlegend=False)
            st.markdown("**From total equipment time to valued operating time**")
            st.plotly_chart(fig, width="stretch", key="oee_waterfall")
            if t.get("nodata"):
                st.caption(f"{t['nodata'] / 3600:.1f} h without machine data counted as unplanned.")
    with b.container(border=True):
        st.markdown("**OEE per machine**")
        sm = oee.station_metrics(e, start, end + timedelta(hours=1), base)
        st_ = stn[stn["line_id"].isin(sel)].merge(sm, left_on="id", right_on="station_id", how="inner")
        if not st_.empty:
            st_["label"] = st_["line_id"].map(c["lines"].set_index("id")["name"]) + " · " + st_["name"]
            st_ = st_.sort_values("oee")
            fig = go.Figure(go.Bar(y=st_["label"], x=st_["oee"] * 100, orientation="h",
                                   marker_color=[_color(v, oee.target_for(tg, l, "oee")) for v, l in zip(st_["oee"], st_["line_id"])],
                                   marker_pattern_shape=[_pattern(v, oee.target_for(tg, l, "oee")) for v, l in zip(st_["oee"], st_["line_id"])],
                                   marker_pattern_fgcolor="#ffffff",
                                   hovertemplate="%{y}: %{x:.0f}%<extra></extra>"))
            fig.update_layout(height=max(260, 24 * len(st_) + 40), margin=dict(l=4, r=4, t=4, b=4), xaxis=dict(range=[0, 100], ticksuffix="%"))
            st.plotly_chart(fig, width="stretch", key="oee_machine")
    with st.container(border=True):
        st.markdown("**OEE per hour · last 24 h**")
        fig = go.Figure()
        for i, lid in enumerate(sel):
            outs = oee.output_stations(stn[stn["line_id"] == lid])
            d = oee.load(e, db.utcnow() - timedelta(hours=24), db.utcnow() + timedelta(hours=1), outs)
            hs = _hour_series(d, None, None, base, zone)
            if len(hs):
                fig.add_scatter(x=hs["label"], y=hs["oee"] * 100, mode="lines+markers", name=c["lines"].set_index("id").loc[lid, "name"],
                                line=dict(color=[BLUE, "#e0861a", "#17907c", "#c2457a"][i % 4], width=2))
        fig.update_layout(height=260, margin=dict(l=4, r=4, t=4, b=4), yaxis=dict(ticksuffix="%", range=[0, 100]),
                          legend=dict(orientation="h", y=-0.25))
        st.plotly_chart(fig, width="stretch", key="oee_24h")
    allrows = pd.concat(dfs.values()) if dfs else pd.DataFrame()
    if not allrows.empty:
        allrows = allrows.copy()
        allrows["hour_local"] = allrows["hour"].dt.tz_localize(logic.UTC).dt.tz_convert(zone).dt.tz_localize(None)
        st.download_button("Download hourly OEE data (CSV)", allrows.to_csv(index=False).encode("utf-8-sig"),
                           "oee_hourly.csv", "text/csv", icon=":material/download:")
