"""Shift report as a PDF: OEE against target, where the time went, OEE per hour, top stops, calls,
batches and handover notes, one section per line. Built with ReportLab (pure Python, no browser needed).

    pdf, filename = report.shift_report(engine, line_ids=[1, 2], shift="Early", prod_date=date(2026, 10, 20), author="Maria")

Informational only: the MES / eBR remains the system of record.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pandas as pd
from reportlab.graphics.shapes import Drawing, Line, Rect, String
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)
from sqlalchemy import select

from . import db, logic, oee

BLUE, RED, GREEN, AMBER, GREY, INK = (colors.HexColor(c) for c in ("#2563eb", "#dc2626", "#16a34a", "#d97706", "#94a3b8", "#0f172a"))
LIGHT = colors.HexColor("#f1f5f9")
W = A4[0] - 30 * mm

ss = getSampleStyleSheet()
H1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, spaceAfter=2, textColor=INK)
H2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=12.5, spaceBefore=10, spaceAfter=4, textColor=INK)
P = ParagraphStyle("p", parent=ss["BodyText"], fontSize=9, leading=12, alignment=TA_LEFT)
SMALL = ParagraphStyle("s", parent=P, fontSize=8, leading=10, textColor=colors.HexColor("#475569"))
CELL = ParagraphStyle("c", parent=P, fontSize=8.2, leading=10)

KPI_ROWS = [("OEE (headline)", "oee"), ("Availability", "availability"), ("Performance", "performance"), ("Quality", "quality"),
            ("OEE1 (line-controllable time)", "oee1"), ("OEE2 (planned production time)", "oee2"),
            ("OEE3 (scheduled time)", "oee3"), ("TCU (calendar time)", "tcu")]


def cfg(engine) -> dict:
    """The configuration tables the report needs (same shape as the app's cached configuration)."""
    out = {f"{k}_all": db.read_df(engine, t) for k, t in (("lines", db.lines), ("stations", db.stations), ("call_types", db.call_types),
                                                         ("reasons", db.reasons), ("events", db.events))}
    out["settings"] = db.get_settings(engine)
    return out


def shift_window(settings: dict, shift: str, prod_date: date):
    """Start and end of a shift on a production date, in local time and in UTC (naive)."""
    zone, shifts = logic.tz(settings), logic.parse_shifts(settings)
    a, b = next(((s[1], s[2]) for s in shifts if s[0] == shift), (shifts[0][1], shifts[0][2]))
    start = datetime.combine(prod_date, a, tzinfo=zone)
    end = datetime.combine(prod_date if a < b else prod_date + timedelta(days=1), b, tzinfo=zone)
    utc = lambda d: d.astimezone(logic.UTC).replace(tzinfo=None)  # noqa: E731
    return start, end, utc(start), utc(end)


def _pct(v, digits=0):
    return "–" if v is None or pd.isna(v) else f"{v * 100:.{digits}f}%"


def _hours(s):
    return f"{s / 3600:.1f} h" if s else "0.0 h"


def _table(rows, widths, header=True, zebra=True, align_right=()):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("FONT", (0, 0), (-1, -1), "Helvetica", 8.2), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
             ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0"))]
    if header:
        style += [("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8.2), ("BACKGROUND", (0, 0), (-1, 0), LIGHT)]
    for c in align_right:
        style.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    t.setStyle(TableStyle(style))
    return t


def _waterfall(times: dict) -> Drawing:
    """Horizontal waterfall: totals in blue, losses in red, scaled to calendar time."""
    rows = [(lbl, times.get(k, 0) or 0, kind) for lbl, k, kind in oee.WATERFALL]
    total = max(1.0, rows[0][1])
    h_row, label_w, bar_w = 11.5, 140, W - 140 - 50
    d = Drawing(W, h_row * len(rows) + 4)
    for i, (lbl, v, kind) in enumerate(rows):
        y = d.height - (i + 1) * h_row
        d.add(String(0, y + 3, lbl, fontName="Helvetica-Bold" if kind == "total" else "Helvetica", fontSize=7.8))
        w = max(0.5, bar_w * v / total)
        x = label_w if kind == "total" else label_w + bar_w * 0  # losses start at the left too, for readability
        d.add(Rect(x, y + 1, w, h_row - 4, fillColor=BLUE if kind == "total" else RED, strokeColor=None))
        d.add(String(x + w + 4, y + 3, _hours(v), fontName="Helvetica", fontSize=7.5, fillColor=colors.HexColor("#334155")))
    return d


def _hour_bars(series: list[tuple[str, float | None]], target: float | None) -> Drawing:
    d = Drawing(W, 70)
    if not series:
        return d
    left, base, top = 24, 14, 64
    n = len(series)
    bw = min(28, (W - left - 6) / n - 3)
    for v, lbl in ((0, "0%"), (0.5, "50%"), (1, "100%")):
        y = base + (top - base) * v
        d.add(Line(left, y, W, y, strokeColor=colors.HexColor("#e2e8f0"), strokeWidth=0.4))
        d.add(String(0, y - 2, lbl, fontName="Helvetica", fontSize=6.5, fillColor=GREY))
    if target:
        y = base + (top - base) * target
        d.add(Line(left, y, W, y, strokeColor=GREY, strokeWidth=0.6, strokeDashArray=[2, 2]))
    for i, (lbl, v) in enumerate(series):
        x = left + 4 + i * (bw + 3)
        h = (top - base) * (v or 0)
        col = GREY if v is None else (GREEN if not target or v >= target else (AMBER if v >= target * 0.9 else RED))
        d.add(Rect(x, base, bw, max(0.5, h), fillColor=col, strokeColor=None))
        d.add(String(x, 3, lbl, fontName="Helvetica", fontSize=6.3, fillColor=colors.HexColor("#475569")))
    return d


def _line_section(engine, c, line_id, start_l, end_l, start, end, base, tg) -> list:
    zone = logic.tz(c["settings"])
    shifts = logic.parse_shifts(c["settings"])
    lines = c["lines_all"].set_index("id")
    stations = c["stations_all"]
    name = lines.loc[line_id, "name"]
    story = [Paragraph(f"{name} <font size=10 color='#64748b'>{lines.loc[line_id].get('area') or ''}</font>", H2)]
    outs = oee.output_stations(stations[(stations["line_id"] == line_id) & stations["active"].astype(bool)])
    df = oee.load(engine, start, end, outs)
    df = df[(df["hour"] >= start.replace(minute=0, second=0)) & (df["hour"] < end)] if not df.empty else df
    m = oee.metrics(df, base)

    # performance vs target
    rows = [["Measure", "Actual", "Target", ""]]
    marks = []
    for lbl, k in KPI_ROWS:
        act, tgt = m.get(k), oee.target_for(tg, line_id, k)
        ok = None if act is None or tgt is None else act >= tgt
        rows.append([lbl, _pct(act, 1), _pct(tgt), "" if ok is None else ("on target" if ok else "below target")])
        marks.append(ok)
    scrap_pct = m["scrap"] / (m["good"] + m["scrap"]) if (m["good"] + m["scrap"]) else None
    rows += [["Good output", f"{m['good']:,.0f}", "", ""], ["Scrap", f"{m['scrap']:,.0f} ({_pct(scrap_pct, 1)})", "", ""]]
    t = _table(rows, [70 * mm, 30 * mm, 25 * mm, 30 * mm], align_right=(1, 2))
    st = [("FONT", (0, 1), (-1, 1), "Helvetica-Bold", 9)]
    for i, ok in enumerate(marks, start=1):
        if ok is not None:
            st.append(("TEXTCOLOR", (3, i), (3, i), GREEN if ok else RED))
    t.setStyle(TableStyle(st))
    story += [t]
    if not m.get("times"):
        story.append(Paragraph("No production data for this line in this shift (not scheduled, or no machine data).", SMALL))
    else:
        nod = m["times"].get("nodata") or 0
        story += [Spacer(1, 6), Paragraph("<b>Where the time went</b>", P), _waterfall(m["times"])]
        if nod > 0:
            story.append(Paragraph(f"{nod / 3600:.1f} h without machine data were counted as unplanned stops.", SMALL))
        # OEE per hour
        series = []
        for hr, g in df.groupby("hour"):
            series.append((pd.Timestamp(hr).tz_localize(logic.UTC).tz_convert(zone).strftime("%H:00"), oee.metrics(g, base)["oee"]))
        story += [Spacer(1, 4), Paragraph("<b>OEE per hour</b> (dotted line = target)", P), _hour_bars(series, oee.target_for(tg, line_id, "oee"))]

    # top stops
    ts = oee.top_stops(engine, c, [line_id], start, end, n=8)
    story.append(Paragraph("<b>Top stops</b> (unplanned and external, longer than a microstop)", P))
    if ts is None or ts.empty:
        story.append(Paragraph("No stops.", SMALL))
    else:
        rows = [["Problem", "Where", "Stops", "Total (min)", "Longest (min)"]]
        rows += [[Paragraph(str(r.event), CELL), r.where.split(" · ")[-1], int(r.stops), f"{r.minutes:.0f}", f"{r.longest:.0f}"]
                 for r in ts.itertuples()]
        story.append(_table(rows, [62 * mm, 45 * mm, 18 * mm, 25 * mm, 25 * mm], align_right=(2, 3, 4)))

    # calls
    calls = db.calls_between(engine, start, end)
    calls = calls[calls["line_id"] == line_id] if not calls.empty else calls
    h = logic.enrich_history(calls, c, zone, shifts, db.utcnow()) if not calls.empty else calls
    k = logic.kpis(h if not calls.empty else pd.DataFrame())
    story.append(Paragraph("<b>Calls</b>", P))
    story.append(Paragraph(
        f"{k['calls']} calls · {k['stops']} line stops · downtime {logic.fmt_min(k['downtime']) if k['calls'] else '0 min'} · "
        f"average response {logic.fmt_min(k['mtta'])} · average time to fix {logic.fmt_min(k['mttr'])} · "
        f"escalated {_pct(k['escalated'])}", SMALL))
    if not calls.empty:
        evn = c["events_all"].set_index("id")["short_name"].to_dict() if not c["events_all"].empty else {}
        rows = [["Time", "Unit", "Problem", "Stop", "Response", "Fixed in", "Reason", "By"]]
        for r in h.sort_values("raised_at").head(40).itertuples():
            prob = evn.get(int(r.event_id), r.type) if pd.notna(getattr(r, "event_id", None)) else r.type
            fixed = (r.resolved_at - r.raised_at).total_seconds() if pd.notna(r.resolved_at) else None
            resp = (r.ack_at - r.raised_at).total_seconds() if pd.notna(r.ack_at) else None
            rows.append([pd.Timestamp(r.raised_at).tz_localize(logic.UTC).tz_convert(zone).strftime("%H:%M"), str(r.station),
                         Paragraph(str(prob), CELL), "yes" if r.line_stop else "", logic.fmt_dur(resp) if resp is not None else "–",
                         logic.fmt_dur(fixed) if fixed is not None else "open", Paragraph(str(r.reason), CELL),
                         Paragraph(str(r.resolved_by if isinstance(r.resolved_by, str) else ""), CELL)])
        story.append(_table(rows, [13 * mm, 24 * mm, 38 * mm, 10 * mm, 17 * mm, 17 * mm, 34 * mm, 27 * mm], align_right=(4, 5)))

    # batches
    with engine.connect() as cx:
        b = pd.read_sql(select(db.batches).where(db.batches.c.line_id == int(line_id), db.batches.c.started_at < end,
                                                 (db.batches.c.ended_at.is_(None)) | (db.batches.c.ended_at > start))
                        .order_by(db.batches.c.started_at), cx, parse_dates=["started_at", "ended_at"])
        notes = pd.read_sql(select(db.handover).where(db.handover.c.line_id == int(line_id), db.handover.c.ts >= start,
                                                      db.handover.c.ts < end + timedelta(minutes=30)).order_by(db.handover.c.ts), cx,
                            parse_dates=["ts"])
    tail = [Paragraph("<b>Batches</b>", P)]
    if b.empty:
        tail.append(Paragraph("No batch recorded.", SMALL))
    else:
        loc = lambda t: "running" if pd.isna(t) else pd.Timestamp(t).tz_localize(logic.UTC).tz_convert(zone).strftime("%d %b %H:%M")  # noqa: E731
        rows = [["Batch", "Product", "Destination", "Planned", "Started", "Ended", "Source"]]
        rows += [[r.batch_no, Paragraph(str(r.product), CELL), r.destination or "–",
                  f"{r.planned_qty:,.0f}" if pd.notna(r.planned_qty) else "–", loc(r.started_at), loc(r.ended_at), r.source]
                 for r in b.itertuples()]
        tail.append(_table(rows, [26 * mm, 46 * mm, 26 * mm, 18 * mm, 22 * mm, 22 * mm, 18 * mm], align_right=(3,)))
    tail.append(Paragraph("<b>Handover notes</b>", P))
    if notes.empty:
        tail.append(Paragraph("No handover note written during this shift.", SMALL))
    else:
        for r in notes.itertuples():
            tail.append(Paragraph(f"<b>{r.author}</b>, {pd.Timestamp(r.ts).tz_localize(logic.UTC).tz_convert(zone):%H:%M}: "
                                   f"{_esc(r.note)}", P))
    story.append(KeepTogether(tail))
    return story


def _esc(s) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def shift_report(engine, line_ids: list[int], shift: str, prod_date: date, author: str = "") -> tuple[bytes, str]:
    c = cfg(engine)
    sett = c["settings"]
    base = sett.get("oee_headline_base", "oee2")
    start_l, end_l, start, end = shift_window(sett, shift, prod_date)
    tg = oee.targets(engine)
    lines = c["lines_all"].set_index("id")
    plant = sett.get("plant_name", "Plant")
    names = ", ".join(str(lines.loc[i, "name"]) for i in line_ids if i in lines.index)
    buf = io.BytesIO()
    generated = datetime.now(logic.tz(sett))

    def frame(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(GREY)
        canvas.drawString(15 * mm, 10 * mm, f"{plant} · shift report · generated {generated:%d %b %Y %H:%M}"
                          + (f" by {author}" if author else "") + " · informational only, not a batch record")
        canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
                            title=f"Shift report {names} {shift} {prod_date}", author=author or "Andon")
    story = [Paragraph(f"Shift report · {shift} shift · {prod_date:%a %d %b %Y}", H1),
             Paragraph(f"{_esc(plant)} · {_esc(names)} · {start_l:%H:%M}–{end_l:%H:%M} ({start_l.tzname()})", SMALL),
             Spacer(1, 6)]
    if len(line_ids) > 1:  # summary across lines first
        rows = [["Line", "OEE", "Target", "Availability", "Performance", "Quality", "Good", "Scrap"]]
        stations = c["stations_all"]
        for lid in line_ids:
            outs = oee.output_stations(stations[(stations["line_id"] == lid) & stations["active"].astype(bool)])
            df = oee.load(engine, start, end, outs)
            df = df[df["hour"] < end] if not df.empty else df
            m = oee.metrics(df, base)
            rows.append([lines.loc[lid, "name"], _pct(m["oee"], 1), _pct(oee.target_for(tg, lid, "oee")), _pct(m["availability"]),
                         _pct(m["performance"]), _pct(m["quality"], 1), f"{m['good']:,.0f}", f"{m['scrap']:,.0f}"])
        story += [Paragraph("Summary", H2), _table(rows, [28 * mm, 20 * mm, 20 * mm, 24 * mm, 24 * mm, 20 * mm, 22 * mm, 20 * mm],
                                                    align_right=(1, 2, 3, 4, 5, 6, 7))]
    for i, lid in enumerate(line_ids):
        if i > 0:
            story.append(PageBreak())
        story += _line_section(engine, c, lid, start_l, end_l, start, end, base, tg)
    story += [Spacer(1, 10), Paragraph(
        "OEE = availability × performance × quality on "
        + ("line-controllable time (OEE1)" if base == "oee1" else "planned production time (OEE2)")
        + ". TCU = valued ÷ calendar time · OEE3 = valued ÷ scheduled · OEE2 = valued ÷ planned production time · "
          "OEE1 = valued ÷ line-controllable time. Which stops are planned or external is set per event in the event catalogue.",
        SMALL)]
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    safe = "-".join(str(lines.loc[i, "name"]).replace(" ", "") for i in line_ids if i in lines.index)[:40]
    return buf.getvalue(), f"shift-report_{safe}_{prod_date:%Y-%m-%d}_{shift}.pdf"
