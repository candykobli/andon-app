"""Machine signals: ingesting readings, the event lifecycle, batches, rules that raise Andon calls, and the simulator.

The same `ingest()` is used by the shop-floor connector (collector/) and by the in-app simulator, so events,
batches and rules behave identically whichever way the data arrives.

Special signal keys
  state        Running | Idle | Stopped | Fault | Planned | Off
  fault_code   machine fault or stop reason, e.g. "E-204 Jam detected" ("" when cleared)
  event        any process/MES/SCADA alarm text, e.g. "FIL-12 Low fill weight" ("" when cleared)
  batch_no, product, destination   running batch on the line (from MES/ERP)
"""
from __future__ import annotations

import math
import random
import zlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pandas as pd
from sqlalchemy import and_, delete, insert, select, update
from sqlalchemy.engine import Engine

from . import db, events, logic, oee

STATES = ["Running", "Idle", "Stopped", "Fault", "Planned", "Off"]
EVENT_KEYS = {"fault_code": "machine", "event": "process"}
BATCH_KEYS = ("batch_no", "product", "destination", "planned_qty")
FAULT_CODES = ["E-101 Gripper not closing", "E-204 Jam detected", "E-310 Sensor timeout",
               "E-412 Motor overload", "E-517 Air pressure low", "E-620 Vision reject limit"]
IDLE_CODES = ["EXT-01 No material from warehouse", "ST-02 Starved by upstream station"]
PROCESS_EVENTS = {"Inspection": ["QA-31 Vision reject rate high", "QA-34 Camera needs recalibration"],
                  "Labeller": ["LB-12 Label roll low", "LB-15 Print quality warning"],
                  "Test": ["TS-21 Leak test fail rate high"]}
STALE_S = 90  # readings older than this are shown as "no data" and ignored by rules


# ------------------------------------------------------------------ reading
def latest_df(engine: Engine) -> pd.DataFrame:
    with engine.connect() as cx:
        return pd.read_sql(select(db.signal_latest), cx, parse_dates=["ts", "changed_at", "hist_at"])


def history_df(engine: Engine, since: datetime, station_ids: list[int] | None = None, keys: list[str] | None = None) -> pd.DataFrame:
    q = select(db.signal_history).where(db.signal_history.c.ts >= since)
    if station_ids:
        q = q.where(db.signal_history.c.station_id.in_([int(s) for s in station_ids]))
    if keys:
        q = q.where(db.signal_history.c.key.in_(keys))
    with engine.connect() as cx:
        return pd.read_sql(q.order_by(db.signal_history.c.ts), cx, parse_dates=["ts"])


def station_snapshot(latest: pd.DataFrame, defs: pd.DataFrame, now: datetime) -> dict:
    """{station_id: {key: {value, label, unit, lo, hi, target, kind, age_s, since_s, alarm}}}"""
    out: dict = {}
    if latest.empty:
        return out
    d = defs.set_index(["station_id", "key"]) if not defs.empty else None
    for r in latest.itertuples():
        info = {}
        if d is not None and (r.station_id, r.key) in d.index:
            info = d.loc[(r.station_id, r.key)].to_dict()
        val = r.text if isinstance(r.text, str) else (None if pd.isna(r.num) else float(r.num))
        alarm = False
        if isinstance(val, float):
            lo, hi = info.get("lo"), info.get("hi")
            alarm = (lo is not None and not pd.isna(lo) and val < lo) or (hi is not None and not pd.isna(hi) and val > hi)
        out.setdefault(int(r.station_id), {})[r.key] = {
            "value": val, "label": info.get("label", r.key), "unit": info.get("unit"), "lo": info.get("lo"),
            "hi": info.get("hi"), "target": info.get("target"), "kind": info.get("kind"),
            "age_s": (now - r.ts).total_seconds(), "since_s": (now - r.changed_at).total_seconds(), "alarm": alarm}
    return out


def open_occurrences(engine: Engine) -> pd.DataFrame:
    """Events happening right now, with their catalogue entry."""
    q = (select(db.event_log, db.events.c.short_name, db.events.c.short_name_da, db.events.c.code, db.events.c.raw_text,
                db.events.c.oee_category, db.events.c.show_on_board, db.events.c.line_stop, db.events.c.priority)
         .join(db.events, db.events.c.id == db.event_log.c.event_id).where(db.event_log.c.ended_at.is_(None)))
    with engine.connect() as cx:
        return pd.read_sql(q, cx, parse_dates=["started_at", "ended_at"])


# ------------------------------------------------------------------ ingest
def ingest(engine: Engine, readings: list[dict], now: datetime | None = None) -> int:
    """readings: [{"station_id": int, "key": str, "value": float | str, "ts": datetime (optional, UTC)}]
    Updates latest values and history, tracks events and batches, evaluates rules. Returns auto-calls raised."""
    if not readings:
        return 0
    now = now or db.utcnow()
    st = db.get_settings(engine)
    every = int(st.get("history_every_s") or 60)
    with engine.begin() as cx:
        station_line = {r.id: r.line_id for r in cx.execute(select(db.stations.c.id, db.stations.c.line_id))}
        existing = {(row.station_id, row.key): row for row in cx.execute(select(db.signal_latest)).all()}
        hist, batch_updates = [], {}
        for r in readings:
            sid, key, ts = int(r["station_id"]), str(r["key"]), r.get("ts") or now
            if sid not in station_line:
                continue
            v = r["value"]
            is_num = isinstance(v, (int, float)) and not isinstance(v, bool) and not (isinstance(v, float) and math.isnan(v))
            if key in EVENT_KEYS or key in BATCH_KEYS or key == "state":
                is_num = False  # always text
            num, text = (float(v), None) if is_num else (None, "" if v is None else str(v)[:80])
            old = existing.get((sid, key))
            old_text = (old.text or "") if old is not None else ""
            if key in EVENT_KEYS and text != old_text:
                _event_change(cx, sid, station_line[sid], EVENT_KEYS[key], old_text, text, ts)
            if key in BATCH_KEYS:
                batch_updates.setdefault(station_line[sid], {})[key] = text
            if old is None:
                cx.execute(insert(db.signal_latest).values(station_id=sid, key=key, num=num, text=text, ts=ts,
                                                           changed_at=ts, hist_at=ts))
                hist.append(dict(station_id=sid, key=key, num=num, text=text, ts=ts))
                existing[(sid, key)] = SimpleNamespace(num=num, text=text, ts=ts, changed_at=ts, hist_at=ts)
                continue
            changed = (text != old.text) if text is not None else (old.num is None or abs((old.num or 0) - num) > 1e-9)
            to_hist = (text is not None and changed) or (num is not None and (old.hist_at is None or (ts - old.hist_at).total_seconds() >= every))
            values = dict(num=num, text=text, ts=ts)
            if changed:
                values["changed_at"] = ts
            if to_hist:
                values["hist_at"] = ts
                hist.append(dict(station_id=sid, key=key, num=num, text=text, ts=ts))
            cx.execute(update(db.signal_latest).where(db.signal_latest.c.station_id == sid, db.signal_latest.c.key == key).values(**values))
            existing[(sid, key)] = SimpleNamespace(num=num, text=text, ts=ts, changed_at=values.get("changed_at", old.changed_at),
                                                   hist_at=values.get("hist_at", old.hist_at))
        if hist:
            cx.execute(insert(db.signal_history), hist)
        for line_id, upd in batch_updates.items():
            _update_batch(cx, line_id, upd, now)
        fired = evaluate_rules(cx, now)
        if random.random() < 0.02:  # prune old history now and then
            days = int(st.get("history_days") or 3)
            cx.execute(delete(db.signal_history).where(db.signal_history.c.ts < now - timedelta(days=days)))
    return fired


def _event_change(cx, sid, line_id, source, old_text, new_text, ts):
    if old_text:
        ev_old = events.resolve(cx, source, old_text)
        events.close_occurrences(cx, sid, ts, event_id=ev_old["id"])
    if new_text:
        ev_new = events.resolve(cx, source, new_text)
        events.open_occurrence(cx, ev_new["id"], line_id, sid, ts)


def _num(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _update_batch(cx, line_id, upd, now):
    cur = cx.execute(select(db.batches).where(db.batches.c.line_id == line_id, db.batches.c.ended_at.is_(None))
                     .order_by(db.batches.c.started_at.desc())).mappings().first()
    bno = upd.get("batch_no")
    if bno is not None and bno.strip() == "":
        if cur:
            cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(ended_at=now))
        return
    if bno and (cur is None or cur["batch_no"] != bno):
        if cur:
            cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(ended_at=now))
        cx.execute(insert(db.batches).values(line_id=line_id, batch_no=bno[:60], product=(upd.get("product") or (cur or {}).get("product") or "Unknown product")[:120],
                                             destination=(upd.get("destination") or None), started_at=now, source="connector", started_by="connector",
                                             planned_qty=_num(upd.get("planned_qty"))))
        return
    if cur:
        changes = {k: upd[k] for k in ("product", "destination") if upd.get(k) and upd[k] != cur.get(k)}
        try:
            if upd.get("planned_qty") and float(upd["planned_qty"]) != (cur.get("planned_qty") or 0):
                changes["planned_qty"] = float(upd["planned_qty"])
        except ValueError:
            pass
        if changes:
            cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(**changes))


def _condition(rule, value, info) -> tuple[bool, str]:
    cond, param = rule.condition, (rule.param or "").strip()
    if cond == "state_is":
        states = [s.strip().lower() for s in param.split(",") if s.strip()] or ["fault"]
        ok = isinstance(value, str) and value.lower() in states
        return ok, f"state {value}"
    if cond == "not_empty":
        return isinstance(value, str) and value.strip() != "", str(value)
    if not isinstance(value, (int, float)):
        return False, ""
    lo, hi, target = info.get("lo"), info.get("hi"), info.get("target")
    if cond in ("outside_limits", "above_hi") and hi is not None and value > hi:
        return True, f"{value:.1f} above {hi:g}"
    if cond in ("outside_limits", "below_lo") and lo is not None and value < lo:
        return True, f"{value:.1f} below {lo:g}"
    if cond == "below_target_pct" and target:
        pct = float(param or 60)
        if value < target * pct / 100:
            return True, f"{value:.0f} vs target {target:g} ({value / target:.0%})"
    return False, ""


def evaluate_rules(cx, now: datetime) -> int:
    rules = cx.execute(select(db.rules).where(db.rules.c.active.is_(True))).all()
    if not rules:
        return 0
    defs = {(r.station_id, r.key): r._asdict() for r in cx.execute(select(db.signal_defs).where(db.signal_defs.c.active.is_(True)))}
    latest = {(r.station_id, r.key): r for r in cx.execute(select(db.signal_latest))}
    station_line = {r.id: r.line_id for r in cx.execute(select(db.stations.c.id, db.stations.c.line_id).where(db.stations.c.active.is_(True)))}
    states = {(r.rule_id, r.station_id): r for r in cx.execute(select(db.rule_state))}
    fired = 0
    for rule in rules:
        targets = [rule.station_id] if rule.station_id else sorted({s for (s, k) in defs if k == rule.signal_key} |
                                                                   ({s for (s, k) in latest if k == rule.signal_key} if rule.signal_key in EVENT_KEYS else set()))
        for sid in targets:
            if sid not in station_line:
                continue
            row = latest.get((sid, rule.signal_key))
            ok, desc = False, ""
            if row is not None and (now - row.ts).total_seconds() <= STALE_S:
                value = row.text if row.text is not None else row.num
                ok, desc = _condition(rule, value, defs.get((sid, rule.signal_key), {}))
                if ok and rule.condition == "below_target_pct":  # only judge speed while the machine runs
                    stt = latest.get((sid, "state"))
                    ok = stt is not None and (stt.text or "").lower() == "running"
            key = (rule.id, sid)
            cur = states.get(key)
            if not ok:
                if cur is not None:
                    cx.execute(delete(db.rule_state).where(db.rule_state.c.rule_id == rule.id, db.rule_state.c.station_id == sid))
                continue
            if cur is None:
                cx.execute(insert(db.rule_state).values(rule_id=rule.id, station_id=sid, since=now, fired=False))
                cur_since, cur_fired = now, False
            else:
                cur_since, cur_fired = cur.since, cur.fired
            if cur_fired or (now - cur_since).total_seconds() < rule.duration_s:
                continue
            cx.execute(update(db.rule_state).where(db.rule_state.c.rule_id == rule.id, db.rule_state.c.station_id == sid).values(fired=True))
            # Which exact event is this? A machine/process code if there is one, otherwise the rule itself.
            text_src = None
            if rule.signal_key in EVENT_KEYS and row is not None and row.text:
                text_src = (EVENT_KEYS[rule.signal_key], row.text)
            elif rule.condition == "state_is":
                fc = latest.get((sid, "fault_code"))
                if fc is not None and fc.text:
                    text_src = ("machine", fc.text)
            if text_src:
                ev = events.resolve(cx, text_src[0], text_src[1])
            else:
                ev = events.resolve(cx, "rule", rule.name, code=f"rule:{rule.id}",
                                    defaults=dict(short_name=events.default_name(rule.name), call_type_id=rule.call_type_id,
                                                  line_stop=bool(rule.line_stopped), oee_category="unplanned"))
            if not ev.get("show_on_board", True) or not ev.get("active", True):
                continue  # logged as an event, but configured not to call anyone
            ctype = ev.get("call_type_id") or rule.call_type_id
            stop = bool(ev.get("line_stop")) if text_src else bool(rule.line_stopped)
            note = f"Auto: {rule.name} ({desc})" + (f" · {text_src[1]}" if text_src and text_src[1] not in desc else "")
            cid = db.raise_call(None, station_line[sid], sid, int(ctype), stop, note, source="auto", rule_id=rule.id, cx=cx,
                                event_id=ev["id"])
            if cid:
                events.open_occurrence(cx, ev["id"], station_line[sid], sid, now, call_id=cid)
                fired += 1
    return fired


# ------------------------------------------------------------------ simulator
def simulate_if_due(engine: Engine, every_s: float = 4.0) -> bool:
    """Background work for the app, run from the auto-refreshing screens: simulator step, OEE roll-up and
    notifications. Each part runs at most at its own interval, whichever open screen gets there first."""
    ran = _simulate_if_due(engine, every_s)
    try:
        from . import notify
        notify.check_if_due(engine)
    except Exception:
        pass
    return ran


def _simulate_if_due(engine: Engine, every_s: float = 4.0) -> bool:
    """Run one simulator step if none ran in the last few seconds. Safe with many open screens:
    the settings row works as a lock, so only one session advances the simulation."""
    st = db.get_settings(engine)
    if st.get("simulator") != "1":
        oee.rollup_if_due(engine)
        return False
    now = db.utcnow()
    last = st.get("sim_last") or "1970-01-01T00:00:00"
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        last_dt = now - timedelta(seconds=every_s)
    if (now - last_dt).total_seconds() < every_s:
        return False
    with engine.begin() as cx:
        claimed = cx.execute(update(db.settings).where(and_(db.settings.c.key == "sim_last", db.settings.c.value == last))
                             .values(value=now.isoformat())).rowcount
    if not claimed:
        return False
    simulate_step(engine, now, min(60.0, (now - last_dt).total_seconds()), st)
    oee.rollup_if_due(engine)
    return True


def _hash(*parts) -> float:
    """Stable pseudo-random number in [0, 1) for a station/signal, the same in every process."""
    return (zlib.crc32("|".join(map(str, parts)).encode()) % 10_000) / 10_000


def simulate_step(engine: Engine, now: datetime, dt: float, st: dict) -> None:
    rng = random.Random()
    defs = db.read_df(engine, db.signal_defs, active_only=True)
    lines = db.read_df(engine, db.lines, active_only=True).set_index("id")
    stations = db.read_df(engine, db.stations, active_only=True)
    stations = stations[stations["line_id"].isin(lines.index)]
    latest = latest_df(engine)
    lat = {(r.station_id, r.key): r for r in latest.itertuples()}
    oc = db.open_calls(engine)
    red = set(oc.loc[oc["stopped_at"].notna(), "station_id"].astype(int)) if not oc.empty else set()
    occ = open_occurrences(engine)
    manual_planned = set(occ.loc[(occ["oee_category"] == "planned") & occ["code"].str.startswith("OP-"), "station_id"].astype(int)) if not occ.empty else set()
    zone, shifts = logic.tz(st), logic.parse_shifts(st)
    now_local = pd.Timestamp(now).tz_localize(logic.UTC).tz_convert(zone).to_pydatetime()
    _, shift_start, _ = logic.current_shift_window(now_local, shifts)
    shift_start_utc = shift_start.astimezone(logic.UTC).replace(tzinfo=None)
    outputs = set(oee.output_stations(stations))
    readings = []
    minute = now.timestamp() / 60
    for lid, line in lines.iterrows():
        sched = oee.scheduled_intervals(line.to_dict(), shifts, zone, now - timedelta(minutes=1), now + timedelta(minutes=1))
        scheduled = oee.in_intervals(now, sched)
        p = oee.plan(int(lid), now)
        changeover = scheduled and p["changeover"]
        for s in stations[stations["line_id"] == lid].itertuples():
            sid = int(s.id)
            sdefs = defs[defs["station_id"] == sid].set_index("key")
            if sdefs.empty:
                continue
            cur = lat.get((sid, "state"))
            state = cur.text if cur is not None and cur.text else "Running"
            in_state = (now - cur.changed_at).total_seconds() if cur is not None else 0
            prev_code = lat.get((sid, "fault_code"))
            prev_code = prev_code.text if prev_code is not None and prev_code.text else ""
            code = ""
            if not scheduled:
                state = "Off"
            elif changeover:
                state, code = "Planned", "PL-10 Changeover"
            elif sid in manual_planned:
                state, code = "Planned", None  # an operator started a planned stop: keep its reason
            elif state in ("Off", "Planned"):
                state = "Running"
            elif state == "Fault":
                if sid not in red and in_state > 45:  # call resolved (or no rule) -> machine restarts
                    state = "Running"
                else:
                    code = prev_code or rng.choice(FAULT_CODES)
            elif state == "Stopped":
                if sid not in red:
                    state = "Running"
            elif state == "Idle":
                if sid in red:
                    state = "Stopped"
                elif rng.random() < dt / 150:
                    state = "Running"
                else:
                    code = prev_code or rng.choice(IDLE_CODES)
            else:  # Running
                if sid in red:
                    state = "Stopped"
                elif rng.random() < dt / (3.0 * 3600):
                    state, code = "Fault", rng.choice(FAULT_CODES)
                elif rng.random() < dt / (2.5 * 3600):
                    state, code = "Idle", rng.choice(IDLE_CODES)
            readings.append({"station_id": sid, "key": "state", "value": state})
            if code is not None:
                readings.append({"station_id": sid, "key": "fault_code", "value": code})
            # occasional process/MES alarms that do not stop the machine
            pev = PROCESS_EVENTS.get(s.name)
            if pev:
                prev_ev = lat.get((sid, "event"))
                prev_ev = prev_ev.text if prev_ev is not None and prev_ev.text else ""
                if state != "Running":
                    new_ev = ""
                elif prev_ev:
                    new_ev = "" if rng.random() < dt / 300 else prev_ev
                else:
                    new_ev = rng.choice(pev) if rng.random() < dt / (4 * 3600) else ""
                readings.append({"station_id": sid, "key": "event", "value": new_ev})
            if sid in outputs:  # the line's batch, as MES would send it
                if changeover or not scheduled:
                    b = {"batch_no": "" if changeover else None}
                else:
                    b = oee.demo_batch(int(lid), line["area"], p["index"])
                for k in BATCH_KEYS:
                    if b.get(k) is not None:
                        readings.append({"station_id": sid, "key": k, "value": b[k]})

            target = float(sdefs.loc["speed", "target"]) if "speed" in sdefs.index and not pd.isna(sdefs.loc["speed", "target"]) else 20.0
            wave = math.sin(minute / (37 + 11 * _hash(sid)) + 6.3 * _hash(sid, "w"))
            slow = 0.55 if wave > 0.993 else 1.0
            speed = target * slow * rng.uniform(0.9, 1.04) if state == "Running" else 0.0
            if "speed" in sdefs.index:
                readings.append({"station_id": sid, "key": "speed", "value": round(speed, 1)})
            scrap_rate = sdefs.loc["scrap", "target"] / 100 if "scrap" in sdefs.index and not pd.isna(sdefs.loc["scrap", "target"]) else 0.015
            made = speed * dt / 60
            for key, share in (("good", 1 - scrap_rate), ("scrap", scrap_rate)):
                if key in sdefs.index:
                    prev = lat.get((sid, key))
                    base = prev.num if prev is not None and prev.num is not None and prev.ts >= shift_start_utc else 0.0
                    readings.append({"station_id": sid, "key": key, "value": round(base + made * share * rng.uniform(0.6, 1.4), 2)})
            for key, d in sdefs[sdefs["kind"] == "process"].iterrows():
                lo, hi = d["lo"], d["hi"]
                mid = (lo + hi) / 2 if not pd.isna(lo) and not pd.isna(hi) else (hi * 0.8 if not pd.isna(hi) else 50)
                span = (hi - lo) / 2 if not pd.isna(lo) and not pd.isna(hi) else max(abs(mid) * 0.2, 1)
                prev = lat.get((sid, key))
                v = prev.num if prev is not None and prev.num is not None else mid
                exc = math.sin(minute / (53 + 17 * _hash(sid, key)) + 9 * _hash(key, sid))
                goal = mid + (1.35 * span if exc > 0.996 else 0) * (1 if _hash(sid, key, "d") > .3 else -1)
                if state != "Running":
                    goal = mid - 0.3 * span
                v = v + (goal - v) * min(1, dt / 40) + rng.gauss(0, span * 0.04)
                readings.append({"station_id": sid, "key": key, "value": round(v, 2)})
    ingest(engine, readings, now)
    if st.get("demo_mode") == "1":
        demo_autoresolve(engine, now)


def demo_autoresolve(engine: Engine, now: datetime) -> None:
    """In demo mode a 'Demo bot' closes old calls, so a public demo does not fill up with forgotten calls."""
    oc = db.open_calls(engine)
    if oc.empty:
        return
    rs = db.read_df(engine, db.reasons, active_only=True)
    for r in oc.itertuples():
        age = (now - r.raised_at).total_seconds() / 60
        limit = (4 + (int(r.id) * 37) % 9) if (getattr(r, "source", "manual") == "auto") else 20
        if age >= limit:
            opts = rs[rs["call_type_id"] == r.call_type_id]
            reason = int(opts.iloc[int(r.id) % len(opts)]["id"]) if not opts.empty else None
            if r.status == "open":
                db.acknowledge(engine, int(r.id), "Demo bot")
            db.resolve(engine, int(r.id), "Demo bot", reason, "Closed automatically in demo mode")
