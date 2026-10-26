"""Batches from MES / ERP: which product and batch each line is running, for the OEE board and the shift report.

Three ways in, all through the shop-floor connector (collector/) or by hand:
  1. a `mes` source that polls a read-only SQL view or a REST endpoint of the MES/ERP
     (Siemens Opcenter, Werum PAS-X, Rockwell FactoryTalk, SAP S/4HANA production orders, a custom MES table…)
  2. HTTPS push: POST /batches to the connector's push endpoint
  3. signal keys batch_no / product / destination / planned_qty on the line's output unit (simple PLC or SCADA tags)
Supervisors can also start and end batches by hand on the OEE board.

A batch record (field names can be renamed in the connector config):
  line         line name or id (required)
  batch_no     batch or order number (required)
  product      product name
  destination  market or customer
  planned_qty  planned good units
  status       started | running | ended | completed | closed   (default: running)
  started_at / ended_at   ISO timestamps (default: now)

These figures are informational; the MES or eBR remains the system of record.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, insert, select, update

from . import db

ENDED = {"ended", "completed", "closed", "finished", "done"}
FIELDS = ("line", "batch_no", "product", "destination", "planned_qty", "status", "started_at", "ended_at")


def _ts(v):
    if v in (None, ""):
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return min(dt.replace(microsecond=0), db.utcnow())  # never in the future


def normalise(rec: dict, fields: dict | None = None) -> dict:
    """Rename fields (fields = {our_name: their_name}) and keep only what we use."""
    fields = fields or {}
    out = {k: rec.get(fields.get(k, k)) for k in FIELDS}
    out["batch_no"] = str(out["batch_no"]).strip()[:60] if out.get("batch_no") not in (None, "") else None
    for k in ("product", "destination"):
        out[k] = str(out[k]).strip()[:120] if out.get(k) not in (None, "") else None
    try:
        out["planned_qty"] = float(out["planned_qty"]) if out.get("planned_qty") not in (None, "") else None
    except (TypeError, ValueError):
        out["planned_qty"] = None
    out["status"] = str(out.get("status") or "running").strip().lower()
    return out


def ingest(engine, records: list[dict], source: str = "mes", fields: dict | None = None) -> dict:
    """Create, update or end batches. Returns counts and any unknown line names."""
    res = {"started": 0, "updated": 0, "ended": 0, "skipped": 0, "unknown_lines": []}
    if not records:
        return res
    now = db.utcnow()
    with engine.begin() as cx:
        lines = cx.execute(select(db.lines.c.id, db.lines.c.name)).all()
        by_name = {str(n).strip().lower(): i for i, n in lines}
        ids = {i for i, _ in lines}
        for raw in records:
            r = normalise(raw, fields)
            ln = r.get("line")
            lid = int(ln) if str(ln).isdigit() and int(ln) in ids else by_name.get(str(ln or "").strip().lower())
            if lid is None:
                res["unknown_lines"].append(str(ln))
                res["skipped"] += 1
                continue
            if not r["batch_no"]:
                res["skipped"] += 1
                continue
            cur = cx.execute(select(db.batches).where(db.batches.c.line_id == lid, db.batches.c.ended_at.is_(None))
                             .order_by(db.batches.c.started_at.desc())).mappings().first()
            same = cur is not None and cur["batch_no"] == r["batch_no"]
            if r["status"] in ENDED or r.get("ended_at"):
                end = _ts(r.get("ended_at")) or now
                if same:
                    cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(ended_at=end))
                    res["ended"] += 1
                else:
                    res["skipped"] += 1  # already ended, or never seen running
                continue
            if same:
                changes = {k: r[k] for k in ("product", "destination", "planned_qty") if r.get(k) is not None and r[k] != cur.get(k)}
                if changes:
                    cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(**changes))
                    res["updated"] += 1
                continue
            # a different batch is now running on the line: close the previous one
            seen = cx.execute(select(func.count()).select_from(db.batches).where(
                db.batches.c.line_id == lid, db.batches.c.batch_no == r["batch_no"])).scalar()
            if seen:  # an old batch number coming back (re-sent message): do not reopen it
                res["skipped"] += 1
                continue
            start = _ts(r.get("started_at")) or now
            if cur:
                cx.execute(update(db.batches).where(db.batches.c.id == cur["id"]).values(ended_at=max(start, cur["started_at"])))
            cx.execute(insert(db.batches).values(line_id=lid, batch_no=r["batch_no"], product=r["product"] or "Unknown product",
                                                 destination=r["destination"], planned_qty=r["planned_qty"], started_at=start,
                                                 source=source[:12], started_by=source.upper()[:80]))
            res["started"] += 1
    return res
