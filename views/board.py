"""Andon board: every unit of the selected lines in green, yellow, red, blue (planned) or grey, with the exact
problem (short event name from the catalogue), the exact location and a live timer.

Two kinds of boards, set up by process engineers / BAs / IT under Configure → Boards:
  line screen        exactly one line: the screen mounted at that line (operators, team leaders)
  management overview several or all lines: offices, line managers, engineers, management (sign-in needed)
TV screens: /board?kiosk=1&b=<board id>&key=<device key>

Readable for colour-blind people: every status has its own icon and word, a line stop is striped, and the light
theme (default) keeps problems dark or bright against calm white tiles. Colour is never the only signal.
"""
from __future__ import annotations

import html
import json

import pandas as pd
import streamlit as st

from andon import db, logic, oee, signals, ui

BOARD_CSS = """
<style>
.an-board {border-radius:14px; padding:18px 18px 10px; font-family:inherit}
.an-bhead {display:flex; flex-wrap:wrap; gap:14px 28px; align-items:center; justify-content:space-between; margin-bottom:14px}
.an-bhead h2 {margin:0; font-size:1.7rem; letter-spacing:-.01em}
.an-bhead .sub {font-size:.95rem}
.an-kpis {display:flex; gap:10px; flex-wrap:wrap}
.an-kpi {border-radius:10px; padding:6px 12px; min-width:110px}
.an-kpi b {display:block; font-size:1.35rem; font-variant-numeric:tabular-nums}
.an-kpi span {font-size:.72rem; text-transform:uppercase; letter-spacing:.06em}
.an-line {display:grid; grid-template-columns: 170px 1fr; gap:12px; align-items:stretch; margin-bottom:12px}
.an-board.single .an-line {grid-template-columns: 1fr}
.an-board.single .an-lname {display:none}
.an-board.single .an-tiles {grid-template-columns: repeat(auto-fill, minmax(230px, 1fr))}
.an-board.single .an-tile {min-height:190px}
.an-board.single .an-tile .big {font-size:1.5rem}
.an-board.single .an-tile .tm {font-size:2.2rem}
@media (max-width: 900px) {.an-line {grid-template-columns: 1fr}}
.an-lname {border-radius:10px; padding:10px 12px; display:flex; flex-direction:column; justify-content:center; gap:4px}
.an-lname b {font-size:1.2rem}
.an-tiles {display:grid; grid-template-columns: repeat(auto-fill, minmax(178px, 1fr)); gap:10px}
.an-tile {border-radius:10px; padding:9px 12px 10px; min-height:140px; display:flex; flex-direction:column; gap:3px;
          border:2px solid transparent; position:relative}
.an-tile .st {font-size:.72rem; font-weight:800; letter-spacing:.07em; text-transform:uppercase; display:flex; gap:6px; align-items:center}
.an-tile .st .ic {font-size:.95rem; line-height:1}
.an-tile .nm {font-weight:700; font-size:1rem}
.an-tile .pr {font-size:.7rem; text-transform:uppercase; letter-spacing:.06em; opacity:.75}
.an-tile .big {font-weight:800; font-size:1.18rem; line-height:1.2}
.an-tile .tm {font-variant-numeric:tabular-nums; font-size:1.6rem; font-weight:800; letter-spacing:-.01em}
.an-tile .sm {font-size:.8rem; opacity:.95}
.an-tile .pv {font-size:.76rem; opacity:.85; margin-top:auto; font-variant-numeric:tabular-nums}
.an-tile .pv i {font-style:normal; font-weight:700; text-decoration:underline; text-decoration-thickness:2px}
.an-tile .esc {align-self:flex-start; font-size:.66rem; font-weight:800; padding:2px 7px; border-radius:999px; margin:1px 0 2px}
.an-flash {animation: anflash 1s steps(2, start) infinite}
@keyframes anflash {50% {outline:4px solid currentColor; outline-offset:-4px}}
@media (prefers-reduced-motion: reduce) {.an-flash {animation:none; outline:4px solid currentColor; outline-offset:-4px}}
.an-strip {margin-top:6px; padding-top:8px; font-size:.86rem}
.an-strip b.h {font-size:.72rem; text-transform:uppercase; letter-spacing:.06em; margin-right:10px}
.an-strip span.it {display:inline-block; margin:0 18px 4px 0}
.an-strip .ic {font-weight:800; margin-right:5px}
.stripes {background-image: repeating-linear-gradient(135deg, var(--stripe) 0 12px, transparent 12px 24px)}

/* light theme (default): calm white tiles, problems stand out by brightness, icon, word and pattern */
.an-board.light {background:#f4f6f9; color:#0f172a}
.light .an-bhead h2 {color:#0f172a} .light .an-bhead .sub {color:#475569}
.light .an-kpi, .light .an-lname {background:#fff; border:1px solid #e2e8f0}
.light .an-kpi b, .light .an-lname b {color:#0f172a} .light .an-kpi span, .light .an-lname small {color:#64748b}
.light .t-green {background:#fff; color:#0f172a; border-color:#e2e8f0; border-left:8px solid #0e7c5a}
.light .t-green .st {color:#0e7c5a}
.light .t-yellow {background:#ffd54a; color:#1a1300; border-color:#a16207}
.light .t-red {background-color:#b42318; color:#fff; border-color:#7a1712; --stripe:rgba(255,255,255,.13)}
.light .t-fault {background-color:#fde4e1; color:#7a1712; border-color:#b42318; --stripe:rgba(180,35,24,.10)}
.light .t-planned {background:#dbeafe; color:#1e3a8a; border-color:#bfdbfe; border-left:8px solid #1d4ed8}
.light .t-idle {background:#eef2f6; color:#334155; border-color:#e2e8f0; border-left:8px solid #94a3b8}
.light .t-off {background:#f8fafc; color:#94a3b8; border:2px dashed #cbd5e1}
.light .t-nodata {background:#f1f5f9; color:#64748b; border:2px dotted #94a3b8}
.light .esc {background:#0f172a; color:#fff}
.light .an-strip {color:#334155; border-top:1px solid #e2e8f0} .light .an-strip b.h {color:#64748b}

/* dark theme (option for screens in dark areas) */
.an-board.dark {background:#0b1220; color:#e6ebf3}
.dark .an-bhead h2 {color:#fff} .dark .an-bhead .sub {color:#94a3b8}
.dark .an-kpi, .dark .an-lname {background:#131c2e; border:1px solid #22304a}
.dark .an-kpi b, .dark .an-lname b {color:#fff} .dark .an-kpi span, .dark .an-lname small {color:#94a3b8}
.dark .t-green {background:#0f2a22; color:#d1fae5; border-left:8px solid #34d399}
.dark .t-yellow {background:#ffd54a; color:#1a1300}
.dark .t-red {background-color:#b42318; color:#fff; --stripe:rgba(255,255,255,.13)}
.dark .t-fault {background-color:#3b1414; color:#fecaca; border-color:#ef4444; --stripe:rgba(239,68,68,.16)}
.dark .t-planned {background:#1e3a8a; color:#dbeafe; border-left:8px solid #93c5fd}
.dark .t-idle {background:#334155; color:#e2e8f0; border-left:8px solid #94a3b8}
.dark .t-off {background:#111827; color:#6b7280; border:2px dashed #374151}
.dark .t-nodata {background:#1e293b; color:#64748b; border:2px dotted #475569}
.dark .esc {background:#fff; color:#0f172a}
.dark .an-strip {color:#cbd5e1; border-top:1px solid #22304a} .dark .an-strip b.h {color:#94a3b8}
</style>
"""

# status: css class, icon, label key. Icons and words make the status readable without colour.
STATUS = {"red": ("t-red stripes", "■", "s_stop"), "yellow": ("t-yellow", "▲", "s_problem"),
          "fault": ("t-fault stripes", "✕", "s_fault"), "planned": ("t-planned", "◆", "s_planned"),
          "idle": ("t-idle", "‖", "s_idle"), "off": ("t-off", "○", "s_off"), "nodata": ("t-nodata", "?", "s_nodata"),
          "green": ("t-green", "●", "s_running")}


def _st(kind, L):
    cls, ic, key = STATUS[kind]
    return cls, f"<div class='st'><span class='ic'>{ic}</span>{L[key]}</div>"



DEFAULT_BOARD = {"id": 0, "name": "All lines", "kind": "overview", "lines": None, "tile_label": "event", "language": "en",
                 "theme": "light", "show_process": True, "show_speed": True, "show_responder": True, "show_ticker": True,
                 "show_recent": True, "show_kpis": True, "sound": True}


def visible_boards(c, kiosk: bool) -> pd.DataFrame:
    """A wall screen shows the board it was set up for. People see line screens; management overviews (several
    lines) only for line managers, viewers, engineers and admins."""
    b = c["boards"]
    if b.empty or kiosk or ui.can("overview"):
        return b
    return b[b["kind"].fillna("overview") == "line"]


def _board_cfg(c, kiosk: bool = False) -> dict | None:
    boards = visible_boards(c, kiosk)
    if c["boards"].empty:
        return dict(DEFAULT_BOARD)
    if boards.empty:
        return None
    want = st.query_params.get("b")
    try:
        want = int(want) if want else None
    except ValueError:
        want = None
    row = boards[boards["id"] == want] if want else boards.head(0)
    return (row if not row.empty else boards.head(1)).iloc[0].to_dict()


def _board_lines(bc) -> list[int] | None:
    if bc.get("lines"):
        try:
            return [int(x) for x in json.loads(bc["lines"])]
        except (ValueError, TypeError):
            return None
    return None


def render():
    ui.require("floor")
    c = ui.cfg()
    kiosk = st.query_params.get("kiosk") == "1"
    bc = _board_cfg(c, kiosk)
    if bc is None:
        st.info("No line screen is set up yet. Process engineers can add one under Configure → Boards.")
        return
    st.session_state["board_id"] = int(bc["id"])
    st.session_state.pop("board_line_filter", None)
    if not kiosk:
        boards = visible_boards(c, kiosk)
        a, b = st.columns([2, 4], vertical_alignment="bottom")
        ids = list(boards["id"].astype(int))
        names = boards.set_index("id")["name"].to_dict()
        if len(ids) > 1:
            pick = a.selectbox("Board", ids, index=ids.index(int(bc["id"])) if int(bc["id"]) in ids else 0,
                               format_func=names.get, key="board_pick")
            if pick != int(bc["id"]):
                st.query_params["b"] = str(pick)
                st.rerun()
        if bc.get("kind", "overview") == "overview" and len(c["lines"]) > 1:
            own = _board_lines(bc) or list(c["lines"]["id"].astype(int))
            lmap = c["lines"].set_index("id")["name"].to_dict()
            show = b.multiselect("Lines shown", [i for i in own if i in lmap], default=[i for i in own if i in lmap],
                                 format_func=lmap.get, key=f"board_lines_{bc['id']}",
                                 help="Hide lines for this view only. The board's set-up stays as it is.")
            st.session_state["board_line_filter"] = show or own
        st.caption(f"TV link: **/board?kiosk=1&b={int(bc['id'])}**" + ("&key=…" if ui.secret("DEVICE_KEY") else "")
                   + (" · line screen: shows only this line" if bc.get("kind") == "line" else " · management overview"))
    _board()
    if bool(bc.get("sound", True)):
        sound_player()


SOUND_JS = """
<div id="wrap" style="font:13px system-ui,sans-serif;color:#64748b;display:flex;gap:10px;align-items:center">
  <button id="btn" style="border:1px solid #cbd5e1;background:#fff;border-radius:8px;padding:4px 10px;cursor:pointer">
    &#128264; Enable sound</button><span id="msg">Plays a chime on a new call, a line stop and an escalation.</span>
</div>
<script>
let ctx = null, last = null;
const btn = document.getElementById('btn'), msg = document.getElementById('msg');
function tone(f, t0, d, v) {
  const o = ctx.createOscillator(), g = ctx.createGain();
  o.type = 'sine'; o.frequency.value = f; o.connect(g); g.connect(ctx.destination);
  const t = ctx.currentTime + t0;
  g.gain.setValueAtTime(0.0001, t); g.gain.exponentialRampToValueAtTime(v, t + 0.02);
  g.gain.exponentialRampToValueAtTime(0.0001, t + d); o.start(t); o.stop(t + d + 0.05);
}
function play(kind) {
  window.andonLastChime = kind;  // lets automated tests check which chime played
  if (!ctx || ctx.state !== 'running') return;
  if (kind === 'red') { [0, 0.5].forEach(s => { tone(880, s, 0.22, 0.5); tone(660, s + 0.24, 0.22, 0.5); }); }
  else if (kind === 'escalation') { [0, 0.18, 0.36].forEach(s => tone(990, s, 0.14, 0.45)); }
  else { tone(660, 0, 0.35, 0.4); }
}
function enabled() { btn.style.display = 'none'; msg.textContent = '🔔 Sound on: chime on new calls, line stops and escalations.'; }
btn.onclick = () => { ctx = ctx || new (window.AudioContext || window.webkitAudioContext)(); ctx.resume().then(() => { enabled(); play('yellow'); }); };
try {  // wall screens started with --autoplay-policy=no-user-gesture-required need no click
  ctx = new (window.AudioContext || window.webkitAudioContext)();
  if (ctx.state === 'running') enabled();
} catch (e) {}
setInterval(() => {
  let el = null;
  try { el = window.parent.document.getElementById('andon-alert'); } catch (e) { return; }
  if (!el) return;
  const s = el.dataset.seq;
  if (last === null) { last = s; return; }
  if (s !== last) { last = s; play(el.dataset.kind); }
}, 1000);
</script>
"""


def sound_player():
    """One small player that stays on the page; the board below tells it when to chime (see _alert)."""
    st.iframe(SOUND_JS, height=38)  # our own fixed HTML, never user input


def _alert(oc) -> str:
    """Compare open calls with the previous refresh: new call (yellow), line stop (red) or escalation.
    Returns a hidden marker; the sound player chimes when its sequence number changes."""
    ss = st.session_state
    now = {int(r.id): (bool(r.stopped), int(r.level or 0)) for r in oc.itertuples()} if not oc.empty else {}
    prev = ss.get("alert_prev")
    kind = None
    if prev is not None:
        for cid, (stopped, level) in now.items():
            if cid not in prev:
                kind = "red" if stopped else (kind or "yellow")
            elif stopped and not prev[cid][0]:
                kind = "red"
            elif level > prev[cid][1] and kind != "red":
                kind = "escalation"
    ss["alert_prev"] = now
    if kind:
        ss["alert_seq"] = ss.get("alert_seq", 0) + 1
        ss["alert_kind"] = kind
    return (f"<div id='andon-alert' data-seq='{ss.get('alert_seq', 0)}' data-kind='{ss.get('alert_kind', '')}' "
            f"style='display:none'></div>")


@st.fragment(run_every="5s")
def _board():
    e = ui.engine()
    signals.simulate_if_due(e)
    c = ui.cfg()
    bid = st.session_state.get("board_id")
    rows = c["boards"][c["boards"]["id"] == bid] if bid is not None and not c["boards"].empty else c["boards"].head(0)
    bc = rows.iloc[0].to_dict() if not rows.empty else (_board_cfg(c, st.query_params.get("kiosk") == "1") or dict(DEFAULT_BOARD))
    lang = bc.get("language") or "en"
    theme = bc.get("theme") or "light"
    L = ui.LABELS.get(lang, ui.LABELS["en"])
    now = db.utcnow()
    zone, shifts = ui.zone(), ui.shifts()
    now_l = ui.now_local()
    sname, s_start, s_end = logic.current_shift_window(now_l, shifts)
    s_start_utc = s_start.astimezone(logic.UTC).replace(tzinfo=None)

    line_ids = _board_lines(bc)
    if st.session_state.get("board_line_filter"):
        line_ids = list(st.session_state["board_line_filter"])
    lines = c["lines"] if not line_ids else c["lines"][c["lines"]["id"].isin(line_ids)]
    ev = c["events"].set_index("id") if not c["events"].empty else pd.DataFrame()

    def ev_name(eid, fallback=""):
        if eid is None or pd.isna(eid) or ev.empty or int(eid) not in ev.index:
            return fallback
        r = ev.loc[int(eid)]
        return (r["short_name_da"] if lang == "da" and isinstance(r.get("short_name_da"), str) and r["short_name_da"] else r["short_name"])

    oc = logic.enrich_open(db.open_calls(e), c, now)
    if not oc.empty:
        oc = oc[oc["line_id"].isin(lines["id"])]
    defs = db.read_df(e, db.signal_defs, active_only=True)
    latest = signals.latest_df(e)
    snap = signals.station_snapshot(latest, defs, now)
    occ = signals.open_occurrences(e)
    hist = logic.enrich_history(db.calls_between(e, s_start_utc, now + pd.Timedelta(minutes=1)), c, zone, shifts, now)
    if not hist.empty:
        hist = hist[hist["line_id"].isin(lines["id"])]
    k = logic.kpis(hist)
    stn = c["stations"]
    om = oee.metrics(oee.load(e, s_start_utc, now + pd.Timedelta(hours=1),
                              [s for s in oee.output_stations(stn[stn["line_id"].isin(lines["id"])])]),
                     c["settings"].get("oee_headline_base", "oee2"))

    def kpi(v, label):
        return f"<div class='an-kpi'><b>{v}</b><span>{html.escape(label)}</span></div>"

    stopped_now = int(oc["stopped"].sum()) if not oc.empty else 0
    single = len(lines) == 1
    parts = [BOARD_CSS, f"<div class='an-board {html.escape(theme)}{' single' if single else ''}'>", "<div class='an-bhead'><div>",
             f"<h2>{html.escape(c['settings'].get('plant_name', 'Plant'))} · {html.escape(str(bc.get('name', '')))}</h2>",
             f"<div class='sub'>{html.escape(sname)} {L['shift']} {s_start:%H:%M}–{s_end:%H:%M} · {now_l:%a %d %b · %H:%M:%S}</div></div>"]
    if bc.get("show_kpis", True):
        parts += ["<div class='an-kpis'>", kpi(len(oc), L["open_calls"]), kpi(stopped_now, L["stopped_now"]),
                  kpi(logic.fmt_min(k["downtime"]) if k["calls"] else "0 min", L["downtime"]),
                  kpi(logic.fmt_min(k["mtta"]), L["response"]), kpi(k["calls"], L["calls_shift"])]
        if om.get("oee") is not None:
            parts.append(kpi(f"{om['oee'] * 100:.0f}%", L["oee_shift"]))
        parts.append("</div>")
    parts.append("</div>")
    if single:  # a line screen: the line's own status in words, big, under the header
        ln = lines.iloc[0]
        lc = oc[oc["line_id"] == ln["id"]] if not oc.empty else oc
        kind = "red" if not lc.empty and lc["stopped"].any() else ("yellow" if not lc.empty else "green")
        cls, st_html = _st(kind, L)
        txt = {"red": L["line_stopped"], "yellow": f"{L['open_calls']}: {len(lc)}", "green": L["line_ok"]}[kind]
        parts.append(f"<div class='an-tile {cls}' style='min-height:0;flex-direction:row;align-items:center;gap:18px;margin-bottom:12px'>"
                     f"{st_html}<div class='big'>{html.escape(str(ln['name']))} · {html.escape(txt)}</div></div>")

    by_station = {sid: g for sid, g in oc.groupby("station_id")} if not oc.empty else {}
    occ_by = {sid: g for sid, g in occ.groupby("station_id")} if not occ.empty else {}
    for ln in lines.itertuples():
        sts = stn[stn["line_id"] == ln.id]
        line_calls = oc[oc["line_id"] == ln.id] if not oc.empty else oc
        ln_state = "Stopped" if not line_calls.empty and line_calls["stopped"].any() else ("Issue" if not line_calls.empty else "OK")
        icon = {"Stopped": "■", "Issue": "▲", "OK": "●"}[ln_state]
        label = {"Stopped": L["line_stopped"], "Issue": L["open_calls"] + f": {len(line_calls)}", "OK": L["no_calls"]}[ln_state]
        parts.append(f"<div class='an-line'><div class='an-lname'><b>{html.escape(ln.name)}</b>"
                     f"<small><b style='font-size:1rem'>{icon}</b> {html.escape(label)}</small>"
                     f"<small>{html.escape(str(ln.area))}</small></div><div class='an-tiles'>")
        for s in sts.itertuples():
            parts.append(_tile(s, by_station.get(s.id), snap.get(int(s.id), {}), occ_by.get(s.id), bc, L, lang, ev_name))
        parts.append("</div></div>")

    if bc.get("show_ticker", True) and not oc.empty:
        parts.append("<div class='an-strip'>")
        for r in oc.head(12).itertuples():
            icon = "■" if r.stopped else "▲"
            who = f"{L['ack']} {html.escape(str(r.ack_by))}" if r.status == "ack" else f"→ {html.escape(r.role)}"
            where = html.escape(str(r.station)) if single else f"{html.escape(str(r.line))} · {html.escape(str(r.station))}"
            parts.append(f"<span class='it'><span class='ic'>{icon}</span>{where} · <b>{html.escape(ev_name(r.event_id, r.type))}</b>"
                         f"{' (' + L['line_stopped'].lower() + ')' if r.stopped else ''} · {logic.fmt_dur(r.age_s)} · {who}</span>")
        parts.append("</div>")
    if bc.get("show_recent", True):
        recent = db.calls_between(e, now - pd.Timedelta(hours=8), now + pd.Timedelta(minutes=1))
        recent = recent[(recent["status"] == "resolved") & recent["resolved_at"].notna() & recent["line_id"].isin(lines["id"])]
        recent = recent[recent["resolved_at"] >= now - pd.Timedelta(minutes=90)].sort_values("resolved_at", ascending=False).head(6)
        if not recent.empty:
            st_names = stn.set_index("id")["name"].to_dict()
            ln_names = c["lines"].set_index("id")["name"].to_dict()
            parts.append(f"<div class='an-strip'><b class='h'>{L['recent']}</b>")
            for r in recent.itertuples():
                dur = (r.resolved_at - (r.stopped_at if pd.notna(r.stopped_at) else r.raised_at)).total_seconds()
                parts.append(f"<span class='it'><span class='ic'>✓</span>{html.escape(str(ln_names.get(r.line_id, '')))} · "
                             f"{html.escape(str(st_names.get(r.station_id, '')))} · {html.escape(ev_name(r.event_id, 'Call'))} · "
                             f"{L['fixed']} {max(1, round(dur / 60))} min</span>")
            parts.append("</div>")
    parts.append("</div>")
    parts.append(_alert(oc))
    if st.query_params.get("kiosk") == "1":
        parts.append("<style>header, [data-testid='stHeader'], [data-testid='stToolbar'] {display:none !important}"
                     ".block-container {padding:12px !important; max-width:100% !important}</style>")
    st.html("".join(parts))


def _tile(s, calls, sig: dict, occ, bc: dict, L: dict, lang: str, ev_name) -> str:
    name = html.escape(s.name)
    proc = f"<div class='pr'>{html.escape(str(s.process))}</div>" if isinstance(s.process, str) and s.process else ""
    state = sig.get("state", {})
    mstate = state.get("value") if state and state.get("age_s", 1e9) <= signals.STALE_S else None
    pv = []
    sp = sig.get("speed")
    if bc.get("show_speed", True) and sp and isinstance(sp.get("value"), float) and mstate == "Running":
        pv.append(f"{sp['value']:.0f}/{sp['target']:g} /min" if sp.get("target") else f"{sp['value']:.0f}/min")
    if bc.get("show_process", True) and mstate not in (None, "Off"):
        for _, v in sig.items():
            if v.get("kind") == "process" and isinstance(v.get("value"), float):
                txt = f"{v['value']:.1f}{v.get('unit') or ''}"
                pv.append(f"<i title='outside limits'>{html.escape(txt)} !</i>" if v["alarm"] else html.escape(txt))
    pv_html = f"<div class='pv'>{' · '.join(pv)}</div>" if pv else ""

    def tile(kind, body, extra_cls=""):
        cls, st_html = _st(kind, L)
        return f"<div class='an-tile {cls}{extra_cls}'>{st_html}<div class='nm'>{name}</div>{proc}{body}</div>"

    if calls is not None and not calls.empty:
        r = calls.iloc[0]
        kind = "red" if r["stopped"] else "yellow"
        escalated = r["status"] == "open" and r["level"] >= 2
        more = f" +{len(calls) - 1}" if len(calls) > 1 else ""
        problem = ev_name(r.get("event_id"), r["type"])
        mode = bc.get("tile_label", "event")
        big = r["type"] if mode == "call_type" else (f"{r['type']} · {problem}" if mode == "both" and problem != r["type"] else problem)
        who = ""
        if bc.get("show_responder", True):
            who = (f"{L['ack']}: {html.escape(str(r['ack_by']))}" if r["status"] == "ack"
                   else f"→ {html.escape(r['role'])}")
        esc = f"<span class='esc'>{L['escalated']} L{int(r['level'])}</span>" if escalated else (
            "<span class='esc'>AUTO</span>" if r.get("source") == "auto" else "")
        return tile(kind, f"{esc}<div class='big'>{html.escape(big)}{more}</div><div class='tm'>{logic.fmt_dur(r['age_s'])}</div>"
                          f"<div class='sm'>{who}</div>{pv_html}", " an-flash" if escalated else "")
    occ_name = ""
    if occ is not None and not occ.empty:
        o = occ.sort_values("started_at").iloc[-1]
        occ_name = (o["short_name_da"] if lang == "da" and isinstance(o.get("short_name_da"), str) and o["short_name_da"] else o["short_name"])
    if mstate is None:
        return tile("nodata", f"{pv_html}")
    if mstate == "Off":
        return tile("off", "")
    if mstate == "Planned":
        return tile("planned", f"<div class='big'>{html.escape(occ_name or L['planned'])}</div>"
                               f"<div class='tm'>{logic.fmt_dur(state.get('since_s'))}</div>")
    if mstate in ("Fault", "Stopped"):
        return tile("fault", f"<div class='big'>{html.escape(occ_name or mstate)}</div>"
                             f"<div class='tm'>{logic.fmt_dur(state.get('since_s'))}</div>{pv_html}")
    if mstate == "Idle":
        return tile("idle", f"<div class='big'>{html.escape(occ_name or L['idle'])}</div>"
                            f"<div class='tm'>{logic.fmt_dur(state.get('since_s'))}</div>{pv_html}")
    extra = f"<div class='sm'>{html.escape(occ_name)}</div>" if occ_name else f"<div class='sm'>{L['no_calls']}</div>"
    return tile("green", f"{extra}{pv_html}")
