"""OEE time model, hourly roll-up and KPI calculation.

Time model (default, the stop categories are editable in the event catalogue):

  Total calendar time (24/7)
   − not scheduled (no shifts planned, weekends)            → Scheduled time            TCU  = valued ÷ calendar
   − planned stops (changeover, cleaning, planned maint.)   → Planned production time   OEE3 = valued ÷ scheduled
   − external stops (no material, no staff, utilities)      → Line-controllable time    OEE2 = valued ÷ planned production
   − unplanned stops (breakdowns, faults, no data)          → Operating time            OEE1 = valued ÷ line-controllable
   − microstops and speed loss                              → Net operating time
   − quality loss (scrap)                                   → Valued operating time = good units ÷ target rate

  Headline OEE = Availability × Performance × Quality on planned production time (= OEE2), or on
  line-controllable time (= OEE1) if the setting oee_headline_base is "oee1".
"""
from __future__ import annotations

import json
import math
import zlib
from datetime import datetime, time, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import and_, delete, insert, select

from . import db, logic

METRICS = ["oee", "tcu", "oee3", "oee2", "oee1", "availability", "performance", "quality"]
METRIC_LABEL = {"oee": "OEE", "tcu": "TCU", "oee3": "OEE3", "oee2": "OEE2", "oee1": "OEE1",
                "availability": "Availability", "performance": "Performance", "quality": "Quality"}
DEFAULT_TARGETS = {"oee": 0.65, "tcu": 0.35, "oee3": 0.52, "oee2": 0.65, "oee1": 0.72,
                   "availability": 0.85, "performance": 0.90, "quality": 0.985}
TIME_COLS = ["calendar_s", "not_scheduled_s", "planned_s", "external_s", "unplanned_s", "micro_s", "nodata_s", "run_s"]


def _h(*parts) -> float:
    return (zlib.crc32("|".join(map(str, parts)).encode()) % 10_000) / 10_000


# ------------------------------------------------------------------ schedule
def line_shifts(line: dict, shifts) -> list:
    names = None
    raw = line.get("scheduled_shifts")
    if raw:
        try:
            names = set(json.loads(raw))
        except (ValueError, TypeError):
            names = None
    return [s for s in shifts if names is None or s[0] in names]


def scheduled_intervals(line: dict, shifts, zone, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Scheduled production intervals (naive UTC) for a line between start and end."""
    out = []
    my_shifts = line_shifts(line, shifts)
    weekends = bool(line.get("weekends"))
    d0 = (pd.Timestamp(start).tz_localize(logic.UTC).tz_convert(zone) - pd.Timedelta(days=1)).date()
    d1 = pd.Timestamp(end).tz_localize(logic.UTC).tz_convert(zone).date()
    d = d0
    while d <= d1:
        if weekends or d.weekday() < 5:
            for _, a, b in my_shifts:
                s_l = datetime.combine(d, a, tzinfo=zone)
                e_l = datetime.combine(d if a < b else d + timedelta(days=1), b, tzinfo=zone)
                s_u = s_l.astimezone(logic.UTC).replace(tzinfo=None)
                e_u = e_l.astimezone(logic.UTC).replace(tzinfo=None)
                lo, hi = max(s_u, start), min(e_u, end)
                if lo < hi:
                    out.append((lo, hi))
        d += timedelta(days=1)
    out.sort()
    merged = []
    for a, b in out:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def in_intervals(t: datetime, intervals) -> bool:
    return any(a <= t < b for a, b in intervals)


# ------------------------------------------------------------------ demo production plan (simulator + history)
PRODUCTS = {
    "Assembly": [("Infusion pump housing", "PH-"), ("Insulin pen body", "PB-"), ("Inhaler cap", "IC-")],
    "Packaging": [("Pen cartons 5x3 ml", "CT-"), ("Vial trays 10x10 ml", "VT-"), ("Blister packs 28", "BL-")],
}
DESTINATIONS = ["Denmark", "Germany", "France", "United States", "Japan", "Brazil", "China", "United Kingdom"]


def plan(line_id: int, t: datetime) -> dict:
    """Deterministic batches and changeovers for the demo, the same in every process and in the history."""
    cycle = 300 + 60 * int(_h(line_id, "c") * 3)          # 5-7 h per batch
    co_len = 30 + 10 * int(_h(line_id, "l") * 2)           # 30-40 min changeover
    offset = int(_h(line_id, "o") * cycle)
    m = t.timestamp() / 60 + offset
    idx = int(m // cycle)
    pos = m - idx * cycle
    start = t - timedelta(minutes=pos)
    return {"index": idx, "changeover": pos < co_len, "batch_start": start + timedelta(minutes=co_len),
            "cycle_start": start, "cycle_end": start + timedelta(minutes=cycle), "co_len": co_len}


def demo_batch(line_id: int, area: str, idx: int) -> dict:
    prods = PRODUCTS.get(area, PRODUCTS["Assembly"])
    product, prefix = prods[idx % len(prods)]
    return {"product": product, "batch_no": f"{prefix}{26}{(idx * 7 + line_id * 131) % 100000:05d}",
            "destination": DESTINATIONS[(idx + line_id) % len(DESTINATIONS)],
            "planned_qty": float(5000 if area == "Assembly" else 18000)}


# ------------------------------------------------------------------ roll-up from live signals
def rollup(engine, start: datetime, end: datetime, keep_before: bool = False) -> int:
    """Recompute hourly OEE buckets for [start, end) from machine states, counters and event categories.
    keep_before: start is the first machine data ever (e.g. right after the demo was seeded); the part of that hour
    before it keeps what is already stored (the demo history) instead of being counted as 'no data'."""
    st = db.get_settings(engine)
    zone, shifts = logic.tz(st), logic.parse_shifts(st)
    micro = float(st.get("microstop_s") or 120)
    lines = db.read_df(engine, db.lines, active_only=True).set_index("id")
    stations = db.read_df(engine, db.stations, active_only=True)
    stations = stations[stations["line_id"].isin(lines.index)]
    defs = db.read_df(engine, db.signal_defs, active_only=True)
    speed = defs[defs["key"] == "speed"].set_index("station_id")["target"].to_dict()
    h0 = start.replace(minute=0, second=0, microsecond=0)
    cut = start if keep_before and start > h0 else h0
    with engine.connect() as cx:
        prev = pd.read_sql(select(db.oee_hourly).where(db.oee_hourly.c.hour == h0), cx) if cut > h0 else pd.DataFrame()
        hist = pd.read_sql(select(db.signal_history).where(db.signal_history.c.ts >= h0 - timedelta(hours=12),
                                                           db.signal_history.c.ts < end,
                                                           db.signal_history.c.key.in_(["state", "good", "scrap"])),
                           cx, parse_dates=["ts"])
        occ = pd.read_sql(select(db.event_log.c.station_id, db.event_log.c.started_at, db.event_log.c.ended_at,
                                 db.events.c.oee_category)
                          .join(db.events, db.events.c.id == db.event_log.c.event_id)
                          .where(db.event_log.c.started_at < end,
                                 (db.event_log.c.ended_at.is_(None)) | (db.event_log.c.ended_at >= h0 - timedelta(hours=1))),
                          cx, parse_dates=["started_at", "ended_at"])
    rows = []
    rank = {"planned": 3, "external": 2, "unplanned": 1}
    for s in stations.itertuples():
        sid = int(s.id)
        line = lines.loc[s.line_id].to_dict()
        sched = scheduled_intervals(line, shifts, zone, h0, end)
        sh = hist[(hist["station_id"] == sid) & (hist["key"] == "state")].sort_values("ts")
        before = sh[sh["ts"] < h0].tail(1)
        during = sh[sh["ts"] >= h0]
        changes = [(h0, before["text"].iloc[0] if len(before) else None)] + list(zip(during["ts"], during["text"]))
        so = occ[occ["station_id"] == sid]
        # breakpoints: hours, schedule edges, state changes
        pts = {h0, cut, end}
        t = h0
        while t < end:
            pts.add(t)
            t += timedelta(hours=1)
        for a, b in sched:
            pts.update([a, b])
        for ts_, _ in changes:
            if h0 <= ts_ < end:
                pts.add(ts_)
        pts = sorted(p for p in pts if h0 <= p <= end)
        samples = np.array(sorted(hist[(hist["station_id"] == sid) & (hist["key"].isin(["good", "state"]))]["ts"].values),
                           dtype="datetime64[ns]")

        def has_data(t):
            if not len(samples):
                return False
            i = np.searchsorted(samples, np.datetime64(t))
            near = [samples[j] for j in (i - 1, i) if 0 <= j < len(samples)]
            return any(abs((np.datetime64(t) - x) / np.timedelta64(1, "s")) <= 300 for x in near)
        seg_full = {}  # length of each state segment (for microstops)
        for i, (ts_, stt) in enumerate(changes):
            nxt = changes[i + 1][0] if i + 1 < len(changes) else end
            seg_full[ts_] = (nxt - ts_).total_seconds()
        buckets: dict = {}
        ci = 0
        for a, b in zip(pts[:-1], pts[1:]):
            dur = (b - a).total_seconds()
            if dur <= 0 or b <= cut:
                continue
            while ci + 1 < len(changes) and changes[ci + 1][0] <= a:
                ci += 1
            seg_start, stt = changes[ci]
            hr = a.replace(minute=0, second=0, microsecond=0)
            bk = buckets.setdefault(hr, {c: 0.0 for c in TIME_COLS})
            bk["calendar_s"] += dur
            mid = a + (b - a) / 2
            if not in_intervals(mid, sched):
                bk["not_scheduled_s"] += dur
            elif stt is None or not has_data(mid):
                bk["nodata_s"] += dur
            elif stt == "Running":
                bk["run_s"] += dur
            elif stt == "Planned":
                bk["planned_s"] += dur
            elif seg_full.get(seg_start, 1e9) < micro and stt in ("Idle", "Stopped", "Fault"):
                bk["micro_s"] += dur
            else:
                cats = so[(so["started_at"] <= mid) & (so["ended_at"].isna() | (so["ended_at"] > mid))]["oee_category"]
                cat = max(cats, key=lambda c: rank.get(c, 0)) if len(cats) else "unplanned"
                bk[{"planned": "planned_s", "external": "external_s"}.get(cat, "unplanned_s")] += dur
        for key in ("good", "scrap"):
            ch = hist[(hist["station_id"] == sid) & (hist["key"] == key)].sort_values("ts")
            vals = ch["num"].to_numpy()
            tss = list(ch["ts"])
            for i in range(1, len(vals)):
                if tss[i] < cut:
                    continue
                inc = vals[i] - vals[i - 1]
                inc = vals[i] if inc < 0 else inc  # counter reset at shift start
                hr = tss[i].replace(minute=0, second=0, microsecond=0)
                if hr in buckets:
                    buckets[hr][key] = buckets[hr].get(key, 0.0) + float(inc)
        if cut > h0 and not prev.empty:
            old = prev[prev["station_id"] == sid]
            if len(old) and float(old["calendar_s"].iloc[0] or 0) > 0:
                frac = min(1.0, (cut - h0).total_seconds() / float(old["calendar_s"].iloc[0]))
                bk = buckets.setdefault(h0, {c: 0.0 for c in TIME_COLS})
                for col in TIME_COLS + ["good", "scrap"]:
                    bk[col] = bk.get(col, 0.0) + frac * float(old[col].iloc[0] or 0)
        for hr, bk in buckets.items():
            rows.append(dict(station_id=sid, hour=hr, line_id=int(s.line_id), good=bk.get("good", 0.0),
                             scrap=bk.get("scrap", 0.0), rate=speed.get(sid), **{c: bk[c] for c in TIME_COLS}))
    if rows:
        hours = sorted({r["hour"] for r in rows})
        with engine.begin() as cx:
            cx.execute(delete(db.oee_hourly).where(db.oee_hourly.c.hour >= hours[0], db.oee_hourly.c.hour <= hours[-1],
                                                   db.oee_hourly.c.station_id.in_([r["station_id"] for r in rows])))
            cx.execute(insert(db.oee_hourly), rows)
    return len(rows)


def rollup_if_due(engine, every_s: int = 60) -> bool:
    """Roll up recent hours at most once a minute, whichever process gets there first.
    After a pause (app asleep, connector down) it catches up from the last roll-up, up to the history retention.
    In demo mode, hours with no simulated data are filled with synthetic history so the demo stays complete."""
    from sqlalchemy import update
    st = db.get_settings(engine)
    now = db.utcnow()
    last = st.get("rollup_last") or "1970-01-01T00:00:00"
    try:
        if (now - datetime.fromisoformat(last)).total_seconds() < every_s:
            return False
    except ValueError:
        pass
    with engine.begin() as cx:
        n = cx.execute(update(db.settings).where(db.settings.c.key == "rollup_last", db.settings.c.value == last)
                       .values(value=now.isoformat())).rowcount
        if not n:
            exists = cx.execute(select(db.settings.c.key).where(db.settings.c.key == "rollup_last")).first()
            if exists:
                return False
            cx.execute(insert(db.settings).values(key="rollup_last", value=now.isoformat()))
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        last_dt = now - timedelta(hours=2)
    keep_days = int(st.get("history_days") or 3)
    if st.get("demo_mode") == "1" and st.get("simulator") == "1":
        from sqlalchemy import func
        with engine.connect() as cx:
            newest = cx.execute(select(func.max(db.oee_hourly.c.hour))).scalar()
        gap_end = (now - timedelta(hours=2)).replace(minute=0, second=0, microsecond=0)
        if newest is not None and newest + timedelta(hours=1) < gap_end:
            backfill(engine, start=newest + timedelta(hours=1), end=gap_end, seed=int(now.timestamp()) % 10_000)
        start = now - timedelta(hours=2)
    else:
        start = max(min(last_dt - timedelta(hours=1), now - timedelta(hours=2)), now - timedelta(days=keep_days))
    first_data = _first_signal(engine)
    keep = False
    if first_data and first_data >= start:
        start, keep = first_data, True
    rollup(engine, start, now, keep_before=keep)
    return True


def _first_signal(engine):
    from sqlalchemy import func
    with engine.connect() as cx:
        return cx.execute(select(func.min(db.signal_history.c.ts))).scalar()


# ------------------------------------------------------------------ reading and KPIs
def load(engine, start: datetime, end: datetime, station_ids=None) -> pd.DataFrame:
    q = select(db.oee_hourly).where(db.oee_hourly.c.hour >= start.replace(minute=0, second=0, microsecond=0),
                                    db.oee_hourly.c.hour < end)
    if station_ids is not None:
        q = q.where(db.oee_hourly.c.station_id.in_([int(s) for s in station_ids]))
    with engine.connect() as cx:
        df = pd.read_sql(q, cx, parse_dates=["hour"])
    if not df.empty:
        df["rate"] = df["rate"].fillna(df["rate"].median() if df["rate"].notna().any() else 20.0)
    return df


def output_stations(stations: pd.DataFrame) -> list[int]:
    """Stations that count a line's output: flagged ones, or the last station of each line."""
    out = []
    for lid, g in stations.groupby("line_id"):
        flagged = g[g["is_output"].fillna(False).astype(bool)]
        out += list((flagged if not flagged.empty else g.sort_values(["sort", "id"]).tail(1))["id"].astype(int))
    return out


def metrics(df: pd.DataFrame, base: str = "oee2") -> dict:
    """KPIs and the time waterfall (seconds) for a set of hourly rows."""
    if df is None or df.empty:
        return {k: None for k in METRICS} | {"good": 0, "scrap": 0, "times": {}}
    t = {c: float(df[c].sum()) for c in TIME_COLS}
    good, scrap = float(df["good"].sum()), float(df["scrap"].sum())
    ideal = (df["good"] / df["rate"] * 60).sum()            # valued operating time (s)
    net = ((df["good"] + df["scrap"]) / df["rate"] * 60).sum()
    calendar = t["calendar_s"]
    scheduled = calendar - t["not_scheduled_s"]
    ppt = scheduled - t["planned_s"]
    lct = ppt - t["external_s"]
    operating = t["run_s"] + t["micro_s"]
    unplanned = max(0.0, lct - operating)
    speed_loss = max(0.0, operating - net)
    quality_loss = max(0.0, net - ideal)

    def r(a, b):
        return (a / b) if b and b > 0 else None
    out = {"tcu": r(ideal, calendar), "oee3": r(ideal, scheduled), "oee2": r(ideal, ppt), "oee1": r(ideal, lct),
           "performance": min(1.0, r(net, operating)) if r(net, operating) is not None else None,
           "quality": r(good, good + scrap), "good": good, "scrap": scrap}
    base_time = lct if base == "oee1" else ppt
    out["availability"] = r(operating, base_time)
    out["oee"] = out["oee1"] if base == "oee1" else out["oee2"]
    out["times"] = {"calendar": calendar, "not_scheduled": t["not_scheduled_s"], "scheduled": scheduled,
                    "planned": t["planned_s"], "ppt": ppt, "external": t["external_s"], "lct": lct,
                    "unplanned": unplanned, "nodata": t["nodata_s"], "operating": operating, "micro": t["micro_s"],
                    "speed": max(0.0, speed_loss - t["micro_s"]), "net": net, "quality_loss": quality_loss, "valued": ideal}
    return out


WATERFALL = [("Total calendar time", "calendar", "total"), ("Not scheduled", "not_scheduled", "loss"),
             ("Scheduled time", "scheduled", "total"), ("Planned stops", "planned", "loss"),
             ("Planned production time", "ppt", "total"), ("External stops", "external", "loss"),
             ("Line-controllable time", "lct", "total"), ("Unplanned stops", "unplanned", "loss"),
             ("Operating time", "operating", "total"), ("Microstops", "micro", "loss"), ("Speed loss", "speed", "loss"),
             ("Net operating time", "net", "total"), ("Quality loss", "quality_loss", "loss"),
             ("Valued operating time", "valued", "total")]


def targets(engine) -> dict:
    """{line_id or None: {metric: target}} with plant defaults."""
    df = db.read_df(engine, db.oee_targets)
    out = {None: dict(DEFAULT_TARGETS)}
    for r in df.itertuples():
        lid = None if pd.isna(r.line_id) else int(r.line_id)
        out.setdefault(lid, dict(out[None]))[r.metric] = float(r.target)
    return out


def target_for(tg: dict, line_id, metric) -> float | None:
    return (tg.get(line_id) or tg.get(None) or {}).get(metric)


def top_stops(e, c: dict, line_ids, start, end, n=10, lang="en") -> pd.DataFrame:
    """Unplanned and external stops in a period, grouped by event and location, longest first.
    c needs settings, stations_all and lines_all (the app's cached configuration, or report.cfg())."""
    micro = float(c["settings"].get("microstop_s") or 120)
    q = (select(db.event_log, db.events.c.short_name, db.events.c.short_name_da, db.events.c.oee_category, db.events.c.code)
         .join(db.events, db.events.c.id == db.event_log.c.event_id)
         .where(db.event_log.c.line_id.in_([int(x) for x in line_ids]), db.event_log.c.started_at < end,
                (db.event_log.c.ended_at.is_(None)) | (db.event_log.c.ended_at >= start)))
    with e.connect() as cx:
        df = pd.read_sql(q, cx, parse_dates=["started_at", "ended_at"])
    if df.empty:
        return df
    df = df[df["oee_category"] != "planned"]
    s = df["started_at"].clip(lower=start)
    en = df["ended_at"].fillna(pd.Timestamp(db.utcnow())).clip(upper=end)
    df["min"] = (en - s).dt.total_seconds() / 60
    df = df[df["min"] * 60 >= micro]
    if df.empty:
        return df
    df["event"] = df["short_name_da"].where((lang == "da") & df["short_name_da"].notna(), df["short_name"])
    st_names = c["stations_all"].set_index("id")["name"]
    ln_names = c["lines_all"].set_index("id")["name"]
    df["where"] = df["line_id"].map(ln_names) + " · " + df["station_id"].map(st_names)
    g = df.groupby(["event", "where"]).agg(stops=("id", "size"), minutes=("min", "sum"), longest=("min", "max"),
                                           ongoing=("ended_at", lambda x: x.isna().any())).reset_index()
    return g.sort_values("minutes", ascending=False).head(n)


# ------------------------------------------------------------------ demo history
def backfill(engine, days: int = 14, seed: int = 11, start: datetime | None = None, end: datetime | None = None) -> int:
    """Hourly OEE history for the demo, consistent with the simulator's schedule and batches."""
    rng = np.random.default_rng(seed)
    st = db.get_settings(engine)
    zone, shifts = logic.tz(st), logic.parse_shifts(st)
    lines = db.read_df(engine, db.lines, active_only=True).set_index("id")
    stations = db.read_df(engine, db.stations, active_only=True)
    defs = db.read_df(engine, db.signal_defs, active_only=True)
    speed = defs[defs["key"] == "speed"].set_index("station_id")["target"].to_dict()
    scrap_t = defs[defs["key"] == "scrap"].set_index("station_id")["target"].to_dict()
    now = db.utcnow()
    end = end or now.replace(minute=0, second=0, microsecond=0)
    start = start or end - timedelta(days=days)
    if start >= end:
        return 0
    rows = []
    for lid, line in lines.iterrows():
        sched = scheduled_intervals(line.to_dict(), shifts, zone, start, end)
        sts = stations[stations["line_id"] == lid]
        # minute grid for schedule and changeovers
        minutes = pd.date_range(start, end, freq="min", inclusive="left")
        sched_mask = np.zeros(len(minutes), bool)
        mi = minutes.values
        for a, b in sched:
            sched_mask |= (mi >= np.datetime64(a)) & (mi < np.datetime64(b))
        co_mask = np.array([plan(int(lid), m.to_pydatetime())["changeover"] for m in minutes]) & sched_mask
        hours = minutes.floor("h")
        per_hour = pd.DataFrame({"hour": hours, "s": sched_mask, "c": co_mask, "n": 1}).groupby("hour").sum()
        for s in sts.itertuples():
            sid = int(s.id)
            rate = float(speed.get(sid) or 20)
            sr = float(scrap_t.get(sid) or 1.5) / 100
            weak = _h(sid, "weak")
            for hr, ph in per_hour.iterrows():
                sched_s = float(ph["s"]) * 60
                planned = float(ph["c"]) * 60
                rem = sched_s - planned
                local_h = pd.Timestamp(hr).tz_localize(logic.UTC).tz_convert(zone).hour
                night = local_h >= 22 or local_h < 6
                if rem > 0:
                    ext = rem * (rng.beta(1.2, 30) + (0.05 if weak > 0.85 else 0))
                    unpl = rem * (rng.beta(1.1, 14) + (0.04 if night else 0) + (0.25 if rng.random() < 0.025 else 0))
                    micro = rem * rng.uniform(0.01, 0.035)
                    ext, unpl = min(ext, rem * 0.5), min(unpl, rem * 0.7)
                    run = max(0.0, rem - ext - unpl - micro)
                    perf = rng.uniform(0.86, 0.97) - (0.03 if night else 0)
                    made = run / 60 * rate * perf
                    srr = sr * rng.uniform(0.6, 1.6)
                    good, scrap = made * (1 - srr), made * srr
                else:
                    ext = unpl = micro = run = good = scrap = 0.0
                cal = float(ph["n"]) * 60  # a partial first or last hour only counts its own minutes
                rows.append(dict(station_id=sid, hour=pd.Timestamp(hr).to_pydatetime(), line_id=int(lid), calendar_s=cal,
                                 not_scheduled_s=cal - sched_s, planned_s=planned, external_s=ext, unplanned_s=unpl,
                                 micro_s=micro, nodata_s=0.0, run_s=run, good=good, scrap=scrap, rate=rate))
    with engine.begin() as cx:
        cx.execute(delete(db.oee_hourly).where(db.oee_hourly.c.hour >= start, db.oee_hourly.c.hour < end))
        for i in range(0, len(rows), 2000):
            cx.execute(insert(db.oee_hourly), rows[i:i + 2000])
    return len(rows)


def backfill_batches(engine, days: int = 14) -> int:
    lines = db.read_df(engine, db.lines, active_only=True)
    now = db.utcnow()
    rows = []
    for ln in lines.itertuples():
        t = now - timedelta(days=days)
        seen = set()
        while t < now:
            p = plan(int(ln.id), t)
            if p["index"] not in seen:
                seen.add(p["index"])
                b = demo_batch(int(ln.id), ln.area, p["index"])
                ended = p["cycle_end"] if p["cycle_end"] <= now else None
                if p["batch_start"] <= now:
                    rows.append(dict(line_id=int(ln.id), started_at=p["batch_start"], ended_at=ended, source="connector",
                                     started_by="MES", **b))
            t = p["cycle_end"] + timedelta(minutes=1)
    if rows:
        with engine.begin() as cx:
            cx.execute(insert(db.batches), rows)
    return len(rows)


def station_metrics(engine, start: datetime, end: datetime, base: str = "oee2") -> pd.DataFrame:
    """KPIs per station for a period (used by the Machines page and the OEE board)."""
    df = load(engine, start, end)
    rows = []
    for sid, g in df.groupby("station_id"):
        m = metrics(g, base)
        rows.append({"station_id": int(sid), **{k: m.get(k) for k in METRICS}, "good": m["good"], "scrap": m["scrap"],
                     "run_min": m["times"].get("operating", 0) / 60, "planned_min": m["times"].get("ppt", 0) / 60})
    return pd.DataFrame(rows)
