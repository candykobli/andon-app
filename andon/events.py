"""Event catalogue: one place where every event, from any source, gets a short name operators understand.

Sources can be machine fault codes (OPC UA, Modbus), MES or SCADA alarms (Kafka, MQTT, SQL, HTTP push),
Andon rules, or operators. The first time an event is seen, the app creates a catalogue entry with a
default short name and a best-guess mapping. A process engineer, BA or IT developer can then rename it
(English and Danish), decide whether it shows on the board, whether it means a line stop, and how it
counts in OEE. The original code and text are always kept for engineers.
"""
from __future__ import annotations

import re

from sqlalchemy import and_, insert, select, update

from . import db

MAX_NAME = 26
CODE_RE = re.compile(r"^\s*([A-Za-z]{1,6}[-_]?\d{1,5}[A-Za-z]?)\b[\s:·\-–—]*(.*)$")

# keyword -> (call type name, line stop, OEE category)
KEYWORDS = [
    (r"changeover|format change|product change|cleaning|clean|cip\b|sip\b|line clearance|planned maint|break|meeting|start.?up",
     None, True, "planned"),
    (r"starv|no material|material|empty|shortage|supply|kanban|missing comp|warehouse|no pallet|no carton|no bottle",
     "Material", False, "external"),
    (r"no operator|no staff|staffing|utilit|power fail|steam|water supply|compressed air supply|it network",
     "Supervisor / help", True, "external"),
    (r"label|print|vision|camera|reject|weight|fill level|underfill|overfill|seal|leak|contamin|out of spec|dimension|"
     r"quality|deviation|glass|particle|temperature|humidity", "Quality", False, "unplanned"),
    (r"jam|stuck|blocked|motor|overload|drive|gripper|servo|axis|bearing|vibration|pressure|air|hydraulic|electrical|"
     r"sensor|timeout|encoder|vacuum|door|guard|e-?stop|emergency|fault|alarm|error", "Maintenance", True, "unplanned"),
    (r"slow|speed|rate", "Supervisor / help", False, "unplanned"),
]


def parse_code(text: str) -> tuple[str, str]:
    """'E-204 Jam detected' -> ('E-204', 'Jam detected'); 'Low fill weight' -> ('LOW FILL WEIGHT', 'Low fill weight')."""
    t = (text or "").strip()
    m = CODE_RE.match(t)
    if m and m.group(2).strip():
        return m.group(1).upper(), m.group(2).strip()
    if m:
        return m.group(1).upper(), m.group(1).upper()
    return re.sub(r"\s+", " ", t.upper())[:60], t


def default_name(raw: str, code: str = "") -> str:
    """A short, plain default: drop the code, tidy spacing and capitals, cut at a word boundary."""
    _, rest = parse_code(raw) if raw else ("", code)
    rest = re.sub(r"[_]+", " ", rest or code or "Event")
    rest = re.sub(r"\s+", " ", rest).strip(" .:-–·")
    if rest.isupper() and len(rest) > 3:
        rest = rest.capitalize()
    rest = rest[:1].upper() + rest[1:]
    if len(rest) > MAX_NAME:
        cut = rest[:MAX_NAME + 1].rsplit(" ", 1)[0]
        rest = (cut if len(cut) >= 10 else rest[:MAX_NAME - 1]).rstrip(" ,;-") + "…"
    return rest or code or "Event"


def classify(raw: str, type_ids: dict[str, int]) -> tuple[int | None, bool, str]:
    t = (raw or "").lower()
    for pattern, type_name, stop, cat in KEYWORDS:
        if re.search(pattern, t):
            return (type_ids.get(type_name) if type_name else type_ids.get("Supervisor / help")), stop, cat
    return type_ids.get("Maintenance"), True, "unplanned"


def resolve(cx, source: str, raw_text: str, code: str | None = None, defaults: dict | None = None) -> dict:
    """Find the catalogue entry for (source, code), creating it with defaults on first sight."""
    if code is None:
        code, _ = parse_code(raw_text)
    code = str(code)[:60]
    row = cx.execute(select(db.events).where(and_(db.events.c.source == source, db.events.c.code == code))).mappings().first()
    if row:
        return dict(row)
    type_ids = {r.name: r.id for r in cx.execute(select(db.call_types.c.id, db.call_types.c.name))}
    ctype, stop, cat = classify(raw_text or code, type_ids)
    values = dict(source=source, code=code, raw_text=(raw_text or code)[:200], short_name=default_name(raw_text, code),
                  short_name_da=None, call_type_id=ctype, line_stop=stop, priority=2 if cat != "planned" else 3,
                  show_on_board=cat != "planned", oee_category=cat, renamed=False, first_seen=db.utcnow(), active=True)
    values.update(defaults or {})
    eid = cx.execute(insert(db.events).values(**values)).inserted_primary_key[0]
    values["id"] = eid
    return values


def name(event: dict | None, lang: str = "en") -> str:
    if not event:
        return ""
    if lang == "da" and event.get("short_name_da"):
        return event["short_name_da"]
    return event.get("short_name") or event.get("code") or ""


# ------------------------------------------------------------------ occurrences (what happened, where, how long)
def open_occurrence(cx, event_id: int, line_id: int, station_id: int, start, call_id: int | None = None) -> int:
    existing = cx.execute(select(db.event_log.c.id).where(
        db.event_log.c.event_id == event_id, db.event_log.c.station_id == station_id, db.event_log.c.ended_at.is_(None))).first()
    if existing:
        if call_id:
            cx.execute(update(db.event_log).where(db.event_log.c.id == existing.id).values(call_id=call_id))
        return existing.id
    return cx.execute(insert(db.event_log).values(event_id=event_id, line_id=line_id, station_id=station_id,
                                                  started_at=start, call_id=call_id)).inserted_primary_key[0]


def close_occurrences(cx, station_id: int, end, event_id: int | None = None, call_id: int | None = None) -> None:
    q = update(db.event_log).where(db.event_log.c.station_id == station_id, db.event_log.c.ended_at.is_(None))
    if event_id is not None:
        q = q.where(db.event_log.c.event_id == event_id)
    if call_id is not None:
        q = q.where(db.event_log.c.call_id == call_id)
    cx.execute(q.values(ended_at=end))
