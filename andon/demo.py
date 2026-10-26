"""Example configuration and realistic call history for demos. All names are invented."""
from __future__ import annotations

import json
from datetime import timedelta

import numpy as np
from sqlalchemy import insert, select
from sqlalchemy.engine import Engine

from . import db, events, oee

LINES = [("Line 1", "Assembly"), ("Line 2", "Assembly"), ("Line 3", "Packaging")]
STATIONS = {
    "Line 1": ["Feed", "Assembly A", "Assembly B", "Test", "Pack-out"],
    "Line 2": ["Feed", "Assembly A", "Assembly B", "Test", "Pack-out"],
    "Line 3": ["Infeed", "Labeller", "Inspection", "Cartoner", "Palletiser"],
}
CALL_TYPES = [
    # name, colour, L1 role, L2 min, L2 role, L3 min, L3 role
    ("Maintenance", "#2563eb", "Maintenance", 5, "Supervisor", 15, "Production manager"),
    ("Quality", "#9333ea", "Quality", 10, "Supervisor", 20, "Production manager"),
    ("Material", "#0891b2", "Logistics", 5, "Team leader", 15, "Supervisor"),
    ("Supervisor / help", "#64748b", "Team leader", 5, "Supervisor", 15, "Production manager"),
]
REASONS = {
    "Maintenance": ["Mechanical failure", "Electrical failure", "Sensor / vision fault", "Jam", "Changeover problem", "Tooling worn"],
    "Quality": ["Dimension out of spec", "Visual defect", "Label / print error", "Contamination", "Missing documentation", "Supplier defect"],
    "Material": ["Component shortage", "Wrong component", "Packaging material missing", "Late delivery from warehouse", "Kanban not refilled"],
    "Supervisor / help": ["Staffing", "Unclear instruction", "Safety concern", "Training needed", "Other"],
}
# name, escalation role, PIN, admin flag, access level
RESPONDERS = [
    ("Maria Jensen", "Team leader", "1111", False, "supervisor"),
    ("Lars Nielsen", "Maintenance", "2222", False, "responder"),
    ("Sofie Hansen", "Quality", "3333", False, "responder"),
    ("Ahmed Ali", "Logistics", "4444", False, "responder"),
    ("Peter Larsen", "Supervisor", "5555", False, "supervisor"),
    ("Camille Martin", "Production manager", "6666", False, "line_manager"),
    ("Jonas Berg", "Team leader", "7777", False, "engineer"),
    ("Ida Holm", "Quality", "8888", False, "viewer"),
    ("Operator (demo)", "Team leader", "1234", False, "operator"),
    ("Admin", "Production manager", "0000", True, "admin"),
]
SCHEDULE = {"Line 1": (["Early", "Late"], False), "Line 2": (["Early", "Late"], False), "Line 3": (None, True)}  # Line 3 runs 24/7
PROCESS_OF = {"Feed": "Production", "Infeed": "Production", "Assembly A": "Assembly", "Assembly B": "Assembly",
              "Test": "Inspection", "Pack-out": "Packaging", "Labeller": "Labelling", "Inspection": "Inspection",
              "Cartoner": "Packaging", "Palletiser": "Palletising"}
OUTPUT = {"Pack-out", "Palletiser"}

# The event catalogue a BA or process engineer would have prepared: code, raw text, EN, DA, type, stop, category, show
CATALOGUE = [
    ("machine", "E-101 Gripper not closing", "Gripper not closing", "Griber lukker ikke", "Maintenance", True, "unplanned", True),
    ("machine", "E-204 Jam detected", "Product jam", "Fastklemt produkt", "Maintenance", True, "unplanned", True),
    ("machine", "E-310 Sensor timeout", "Sensor not responding", "Sensor svarer ikke", "Maintenance", True, "unplanned", True),
    ("machine", "E-412 Motor overload", "Motor overload", "Motor overbelastet", "Maintenance", True, "unplanned", True),
    ("machine", "E-517 Air pressure low", "Low air pressure", "Lavt lufttryk", "Maintenance", True, "unplanned", True),
    ("machine", "E-620 Vision reject limit", "Too many camera rejects", "For mange kameraafvisninger", "Quality", True, "unplanned", True),
    ("machine", "EXT-01 No material from warehouse", "No material (warehouse)", "Mangler materiale (lager)", "Material", False, "external", True),
    ("machine", "ST-02 Starved by upstream station", "Waiting for upstream", "Venter på forrige station", "Supervisor / help", False, "unplanned", False),
    ("machine", "PL-10 Changeover", "Changeover", "Omstilling", None, True, "planned", False),
    ("machine", "OP-01 Cleaning / line clearance", "Cleaning / line clearance", "Rengøring / line clearance", None, True, "planned", False),
    ("machine", "OP-02 Planned maintenance", "Planned maintenance", "Planlagt vedligehold", None, True, "planned", False),
    ("machine", "OP-03 Format change", "Format change", "Formatskift", None, True, "planned", False),
    ("machine", "OP-04 Team meeting", "Team meeting", "Teammøde", None, True, "planned", False),
    ("process", "QA-31 Vision reject rate high", "High reject rate", "Høj afvisningsrate", "Quality", False, "unplanned", True),
    ("process", "LB-12 Label roll low", "Label roll low", "Etiketrulle næsten tom", "Material", False, "unplanned", True),
    ("process", "TS-21 Leak test fail rate high", "Leak test failures", "Fejl i tæthedstest", "Quality", False, "unplanned", True),
    # left with the app's default name on purpose, to show what a new, not yet reviewed event looks like
    ("process", "QA-34 Camera needs recalibration", None, None, "Quality", False, "unplanned", True),
    ("process", "LB-15 Print quality warning", None, None, "Quality", False, "unplanned", True),
]
CALL_DA = {"Maintenance": "Vedligehold", "Quality": "Kvalitet", "Material": "Materiale", "Supervisor / help": "Hjælp / supervisor"}
RULE_NAMES = {"Machine fault": ("Machine fault", "Maskinfejl"), "Starved (no material)": ("No material", "Mangler materiale"),
              "Temperature out of limits": ("Temperature out of range", "Temperatur uden for grænser"),
              "Pressure out of limits": ("Pressure out of range", "Tryk uden for grænser"),
              "High vibration": ("High vibration", "Høj vibration"), "Running slow": ("Running slow", "Kører langsomt")}


def seed_config(engine: Engine) -> None:
    with engine.begin() as cx:
        for i, (name, area) in enumerate(LINES):
            shifts_, weekends = SCHEDULE.get(name, (None, False))
            lid = cx.execute(insert(db.lines).values(name=name, area=area, sort=i, active=True, weekends=weekends,
                                                     scheduled_shifts=json.dumps(shifts_) if shifts_ else None)).inserted_primary_key[0]
            for j, s in enumerate(STATIONS[name]):
                cx.execute(insert(db.stations).values(line_id=lid, name=s, sort=j, active=True, process=PROCESS_OF.get(s),
                                                      is_output=s in OUTPUT))
        for i, (name, color, r1, m2, r2, m3, r3) in enumerate(CALL_TYPES):
            cid = cx.execute(insert(db.call_types).values(name=name, color=color, l1_role=r1, l2_min=m2, l2_role=r2,
                                                          l3_min=m3, l3_role=r3, sort=i, active=True)).inserted_primary_key[0]
            for r in REASONS[name]:
                cx.execute(insert(db.reasons).values(call_type_id=cid, name=r, active=True))
        now = db.utcnow()
        for name, role, pin, admin, level in RESPONDERS:
            cx.execute(insert(db.responders).values(name=name, role=role, pin_hash=db.hash_pin(pin), is_admin=admin, active=True,
                                                    level=level, status="active", decided_by="Demo setup", decided_at=now))
        cx.execute(insert(db.responders).values(  # an access request waiting for approval
            name="Nadia Rahman", email="nadia.rahman@example.com", role="Team leader", pin_hash=db.hash_pin("4321"),
            is_admin=False, active=True, level="engineer", status="pending", requested_at=now - timedelta(hours=3),
            request_note="BA on the Andon rollout for the packaging hall. Needs to configure the event catalogue and boards."))


def _config(engine):
    with engine.connect() as cx:
        st = cx.execute(select(db.stations.c.id, db.stations.c.line_id, db.stations.c.name)).all()
        ln = {r.id: r.name for r in cx.execute(select(db.lines.c.id, db.lines.c.name))}
        ct = {r.name: r.id for r in cx.execute(select(db.call_types.c.id, db.call_types.c.name))}
        rs = {}
        for r in cx.execute(select(db.reasons.c.id, db.reasons.c.call_type_id, db.reasons.c.name)):
            rs.setdefault(r.call_type_id, []).append((r.id, r.name))
        people = {r.role: r.name for r in cx.execute(select(db.responders.c.name, db.responders.c.role).where(db.responders.c.status == "active").order_by(db.responders.c.id.desc()))}
    return st, ln, ct, rs, people


def seed_history(engine: Engine, days: int = 30, seed: int = 7) -> int:
    """Generate past calls with patterns worth finding: Line 3 Inspection has quality problems, the night
    shift responds more slowly, jams dominate maintenance downtime, and material calls rise in the last week."""
    rng = np.random.default_rng(seed)
    st, ln, ct, rs, people = _config(engine)
    now = db.utcnow()
    start = now - timedelta(days=days)
    type_w = {"Maintenance": .38, "Quality": .22, "Material": .27, "Supervisor / help": .13}
    reason_w = {"Maintenance": [.16, .12, .14, .34, .14, .10], "Quality": [.25, .25, .2, .08, .12, .1],
                "Material": [.4, .15, .15, .18, .12], "Supervisor / help": [.3, .25, .1, .2, .15]}
    role_for = {"Maintenance": "Maintenance", "Quality": "Quality", "Material": "Logistics", "Supervisor / help": "Team leader"}
    rows = []
    t = start
    while t < now - timedelta(minutes=30):
        t += timedelta(minutes=float(rng.exponential(55)))
        hour = (t.hour + 2) % 24  # rough local hour for patterns
        night = hour >= 22 or hour < 6
        weekend = t.weekday() >= 5
        if weekend and rng.random() < .6:
            continue
        sid, lid, sname = st[rng.integers(len(st))]
        recent = (now - t).days < 7
        w = dict(type_w)
        if recent:
            w["Material"] *= 1.8
        if ln[lid] == "Line 3" and sname == "Inspection":
            w["Quality"] *= 4
        names = list(w)
        p = np.array([w[n] for n in names]); p /= p.sum()
        tname = names[rng.choice(len(names), p=p)]
        stopped = rng.random() < {"Maintenance": .62, "Quality": .35, "Material": .5, "Supervisor / help": .15}[tname]
        mtta = float(rng.lognormal(np.log(2.6 if not night else 5.5), .6))
        work = float(rng.lognormal(np.log({"Maintenance": 16, "Quality": 11, "Material": 9, "Supervisor / help": 6}[tname]), .7))
        r_opts = rs[ct[tname]]
        rid = r_opts[rng.choice(len(r_opts), p=np.array(reason_w[tname]) / sum(reason_w[tname]))][0]
        rname = dict(r_opts)[rid]
        if rname == "Jam":
            work *= 0.6
        if rname in ("Mechanical failure", "Electrical failure"):
            work *= 2.2
        ack = t + timedelta(minutes=mtta)
        res = ack + timedelta(minutes=work)
        if res >= now:
            continue
        resolver = people.get(role_for[tname], "Maria Jensen")
        rows.append(dict(line_id=lid, station_id=sid, call_type_id=ct[tname], status="resolved",
                         raised_at=t.replace(microsecond=0), stopped_at=(t + timedelta(minutes=float(rng.uniform(0, 1.5)))).replace(microsecond=0) if stopped else None,
                         ack_at=ack.replace(microsecond=0), ack_by=resolver, resolved_at=res.replace(microsecond=0),
                         resolved_by=resolver, reason_id=rid, note=None, resolution_note=None))
    if rows:
        with engine.begin() as cx:
            pool = _event_pool(cx, ct)
            for r in rows:
                tname = next(k for k, v in ct.items() if v == r["call_type_id"])
                opts = pool.get(tname) or []
                if opts:
                    ids, w = zip(*opts)
                    r["event_id"] = int(rng.choice(ids, p=np.array(w) / sum(w)))
                cid = cx.execute(insert(db.calls).values(**r)).inserted_primary_key[0]
                if r.get("event_id"):
                    cx.execute(insert(db.event_log).values(event_id=r["event_id"], line_id=r["line_id"], station_id=r["station_id"],
                                                           started_at=r["stopped_at"] or r["raised_at"], ended_at=r["resolved_at"],
                                                           call_id=cid))
    return len(rows)


def _event_pool(cx, ct) -> dict:
    """Which catalogue events a call of each type is likely to be, with weights."""
    ev = {r.code: r.id for r in cx.execute(select(db.events.c.code, db.events.c.id))}
    pick = {"Maintenance": [("E-204", 5), ("E-101", 2), ("E-310", 2), ("E-412", 1.5), ("E-517", 1.5)],
            "Quality": [("E-620", 2), ("QA-31", 3), ("TS-21", 1.5), ("LB-15", 1)],
            "Material": [("EXT-01", 4), ("LB-12", 2)],
            "Supervisor / help": [(f"call:{ct['Supervisor / help']}", 1)]}
    return {t: [(ev[c], w) for c, w in lst if c in ev] for t, lst in pick.items()}


def seed_live(engine: Engine) -> None:
    """A few open calls so the board is not all green on first open."""
    st, ln, ct, rs, people = _config(engine)
    by = {(ln[l], s): (sid, l) for sid, l, s in st}
    now = db.utcnow()
    live = [(("Line 1", "Assembly B"), "Maintenance", True, 7.5, None),
            (("Line 2", "Feed"), "Material", False, 2.2, None),
            (("Line 3", "Inspection"), "Quality", False, 4.0, ("Sofie Hansen", 1.5))]
    rows = []
    with engine.connect() as cx:
        evc = {r.code: r.id for r in cx.execute(select(db.events.c.code, db.events.c.id))}
    live_event = {"Maintenance": "E-101", "Material": "EXT-01", "Quality": "QA-31"}
    for (line, station), tname, stopped, mins_ago, ack in live:
        if (line, station) not in by:
            continue
        sid, lid = by[(line, station)]
        raised = now - timedelta(minutes=mins_ago)
        rows.append(dict(line_id=lid, station_id=sid, call_type_id=ct[tname], status="ack" if ack else "open",
                         raised_at=raised, stopped_at=raised if stopped else None,
                         ack_at=raised + timedelta(minutes=ack[1]) if ack else None, ack_by=ack[0] if ack else None,
                         note="Gripper not closing" if tname == "Maintenance" else None,
                         event_id=evc.get(live_event[tname]), source="manual"))
    if rows:
        with engine.begin() as cx:
            for r in rows:
                cid = cx.execute(insert(db.calls).values(**r)).inserted_primary_key[0]
                if r["event_id"]:
                    cx.execute(insert(db.event_log).values(event_id=r["event_id"], line_id=r["line_id"], station_id=r["station_id"],
                                                           started_at=r["raised_at"], call_id=cid))


# signal definitions: (key, label, kind, unit, lo, hi, target)
BASE_SIGNALS = [("state", "Machine state", "state", None, None, None, None),
                ("fault_code", "Fault code", "state", None, None, None, None),
                ("speed", "Speed", "rate", "units/min", None, None, None),
                ("good", "Good this shift", "counter", "units", None, None, None),
                ("scrap", "Scrap this shift", "counter", "units", None, None, 1.5)]  # target = expected scrap %
PROCESS = {  # station name -> extra process values
    "Feed": [("temperature", "Motor temperature", "°C", 30, 65)],
    "Infeed": [("temperature", "Motor temperature", "°C", 30, 65)],
    "Assembly A": [("temperature", "Press temperature", "°C", 40, 70), ("pressure", "Air pressure", "bar", 5.5, 6.8)],
    "Assembly B": [("temperature", "Press temperature", "°C", 40, 70), ("pressure", "Air pressure", "bar", 5.5, 6.8)],
    "Test": [("vibration", "Vibration", "mm/s", None, 4.5)],
    "Labeller": [("temperature", "Glue temperature", "°C", 150, 170)],
    "Inspection": [("temperature", "Camera housing temp.", "°C", 20, 40)],
    "Pack-out": [("temperature", "Sealer temperature", "°C", 135, 155)],
    "Cartoner": [("pressure", "Vacuum", "bar", 0.6, 0.85)],
    "Palletiser": [("vibration", "Vibration", "mm/s", None, 6.0)],
}
SPEED = {"Line 1": 22, "Line 2": 22, "Line 3": 60}


def seed_signals(engine: Engine) -> None:
    with engine.begin() as cx:
        st = cx.execute(select(db.stations.c.id, db.stations.c.name, db.lines.c.name.label("line"))
                        .join(db.lines, db.lines.c.id == db.stations.c.line_id)).all()
        ct = {r.name: r.id for r in cx.execute(select(db.call_types.c.id, db.call_types.c.name))}
        rows = []
        for s in st:
            sort = 0
            for key, label, kind, unit, lo, hi, target in BASE_SIGNALS:
                if key == "speed":
                    target = SPEED.get(s.line, 20)
                if key == "scrap" and s.name == "Inspection":
                    target = 4.0
                rows.append(dict(station_id=s.id, key=key, label=label, kind=kind, unit=unit, lo=lo, hi=hi,
                                 target=target, sort=sort, active=True))
                sort += 1
            for key, label, unit, lo, hi in PROCESS.get(s.name, []):
                rows.append(dict(station_id=s.id, key=key, label=label, kind="process", unit=unit, lo=lo, hi=hi,
                                 target=None, sort=sort, active=True))
                sort += 1
        cx.execute(insert(db.signal_defs), rows)
        cx.execute(insert(db.rules), [
            dict(name="Machine fault", signal_key="state", condition="state_is", param="Fault", duration_s=30,
                 call_type_id=ct["Maintenance"], line_stopped=True, station_id=None, active=True),
            dict(name="Starved (no material)", signal_key="state", condition="state_is", param="Idle", duration_s=120,
                 call_type_id=ct["Material"], line_stopped=False, station_id=None, active=True),
            dict(name="Temperature out of limits", signal_key="temperature", condition="outside_limits", param=None,
                 duration_s=60, call_type_id=ct["Quality"], line_stopped=False, station_id=None, active=True),
            dict(name="Pressure out of limits", signal_key="pressure", condition="outside_limits", param=None,
                 duration_s=60, call_type_id=ct["Maintenance"], line_stopped=False, station_id=None, active=True),
            dict(name="High vibration", signal_key="vibration", condition="above_hi", param=None,
                 duration_s=60, call_type_id=ct["Maintenance"], line_stopped=False, station_id=None, active=True),
            dict(name="Running slow", signal_key="speed", condition="below_target_pct", param="70",
                 duration_s=180, call_type_id=ct["Supervisor / help"], line_stopped=False, station_id=None, active=True),
            dict(name="Process alarm", signal_key="event", condition="not_empty", param=None,
                 duration_s=0, call_type_id=ct["Quality"], line_stopped=False, station_id=None, active=True),
        ])


def seed_events(engine: Engine) -> None:
    with engine.begin() as cx:
        ct = {r.name: r.id for r in cx.execute(select(db.call_types.c.id, db.call_types.c.name))}
        now = db.utcnow()
        for source, raw, en, da, tname, stop, cat, show in CATALOGUE:
            code, _ = events.parse_code(raw)
            defaults = dict(call_type_id=ct.get(tname), line_stop=stop, oee_category=cat, show_on_board=show,
                            priority=1 if stop and cat == "unplanned" else (3 if cat == "planned" else 2))
            if en:
                defaults.update(short_name=en, short_name_da=da, renamed=True, renamed_by="Demo setup", renamed_at=now)
            events.resolve(cx, source, raw, code=code, defaults=defaults)
        for tname, tid in ct.items():
            events.resolve(cx, "operator", tname, code=f"call:{tid}",
                           defaults=dict(short_name=tname, short_name_da=CALL_DA.get(tname), call_type_id=tid,
                                         line_stop=False, oee_category="unplanned", renamed=True, renamed_by="Demo setup"))
        for r in cx.execute(select(db.rules)).all():
            en, da = RULE_NAMES.get(r.name, (r.name, None))
            events.resolve(cx, "rule", r.name, code=f"rule:{r.id}",
                           defaults=dict(short_name=en, short_name_da=da, call_type_id=r.call_type_id, line_stop=bool(r.line_stopped),
                                         oee_category="external" if "material" in r.name.lower() else "unplanned",
                                         renamed=True, renamed_by="Demo setup"))


def seed_boards_targets(engine: Engine) -> None:
    with engine.begin() as cx:
        ln = {r.name: r.id for r in cx.execute(select(db.lines.c.id, db.lines.c.name))}
        common = dict(active=True, show_process=True, show_speed=True, show_responder=True, show_ticker=True, show_recent=True,
                      show_kpis=True, theme="light")
        cx.execute(insert(db.boards), [  # one screen per line on the shop floor, one overview for managers
            dict(name="Line 1 screen", kind="line", lines=json.dumps([ln["Line 1"]]), tile_label="event", language="en", sort=0, **common),
            dict(name="Line 2 screen", kind="line", lines=json.dumps([ln["Line 2"]]), tile_label="both", language="en", sort=1, **common),
            dict(name="Line 3 skærm (dansk)", kind="line", lines=json.dumps([ln["Line 3"]]), tile_label="event", language="da", sort=2,
                 **common),
            dict(name="Management overview", kind="overview", lines=None, tile_label="event", language="en", sort=3, **common),
        ])
        rows = [dict(line_id=None, metric=m, target=t) for m, t in oee.DEFAULT_TARGETS.items()]
        rows += [dict(line_id=ln["Line 3"], metric=m, target=t) for m, t in
                 {"oee": 0.70, "oee2": 0.70, "oee1": 0.76, "oee3": 0.60, "tcu": 0.48, "performance": 0.92}.items()]
        cx.execute(insert(db.oee_targets), rows)


def seed_notify(engine: Engine) -> None:
    """Demo channels are 'log only': the messages are written to the notification log, nothing leaves the app."""
    with engine.begin() as cx:
        cx.execute(insert(db.notify_channels), [
            dict(name="Teams · Maintenance channel", kind="log", trigger="all", roles=json.dumps(["Maintenance"]), lines=None, active=True),
            dict(name="SMS · Supervisors on escalation", kind="log", trigger="escalation",
                 roles=json.dumps(["Supervisor", "Production manager"]), lines=None, active=True),
            dict(name="Teams · Every line stop", kind="log", trigger="red", roles=None, lines=None, active=True)])


def seed_handover(engine: Engine) -> None:
    with engine.begin() as cx:
        ln = {r.name: r.id for r in cx.execute(select(db.lines.c.id, db.lines.c.name))}
        now = db.utcnow()
        cx.execute(insert(db.handover), [
            dict(line_id=ln["Line 1"], ts=now - timedelta(hours=2), shift="Early", author="Maria Jensen",
                 note="Gripper on Assembly B replaced at 09:40. Keep an eye on air pressure, it dropped twice."),
            dict(line_id=ln["Line 3"], ts=now - timedelta(hours=1), shift="Early", author="Peter Larsen",
                 note="Camera on Inspection rejects more than usual after the format change. Quality informed."),
        ])


def seed_all(engine: Engine) -> None:
    """The complete demo plant: configuration, users, signals, rules, event catalogue, boards, targets,
    30 days of calls, 14 days of OEE and batches, open calls and handover notes."""
    seed_config(engine)
    seed_signals(engine)
    seed_events(engine)
    seed_boards_targets(engine)
    seed_history(engine, days=30)
    seed_live(engine)
    oee.backfill(engine, days=14, end=db.utcnow())  # up to now, so the current shift already has figures
    oee.backfill_batches(engine, days=14)
    seed_handover(engine)
    seed_notify(engine)
