"""Shift calendar, escalation and KPI calculations. Pure pandas, no Streamlit."""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

UTC = ZoneInfo("UTC")


# ------------------------------------------------------------------ time
def tz(settings: dict) -> ZoneInfo:
    try:
        return ZoneInfo(settings.get("timezone") or "Europe/Copenhagen")
    except Exception:
        return ZoneInfo("Europe/Copenhagen")


def to_local(ts, zone: ZoneInfo):
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).tz_localize(UTC).tz_convert(zone)


def local_to_utc(dt: datetime, zone: ZoneInfo) -> datetime:
    return dt.replace(tzinfo=zone).astimezone(UTC).replace(tzinfo=None)


def parse_shifts(settings: dict) -> list[tuple[str, time, time]]:
    try:
        raw = json.loads(settings.get("shifts") or "[]")
        out = [(n, time.fromisoformat(a), time.fromisoformat(b)) for n, a, b in raw]
        return out or [("Day", time(0), time(0))]
    except Exception:
        return [("Day", time(0), time(0))]


def shift_of(local_dt: datetime, shifts) -> tuple[str, date]:
    """Return (shift name, production date). A night shift that crosses midnight belongs to the date it started."""
    t = local_dt.time()
    for name, a, b in shifts:
        if a < b and a <= t < b:
            return name, local_dt.date()
        if a >= b:  # crosses midnight (or 24h shift when equal)
            if t >= a:
                return name, local_dt.date()
            if t < b:
                return name, local_dt.date() - timedelta(days=1)
    return shifts[0][0], local_dt.date()


def current_shift_window(now_local: datetime, shifts) -> tuple[str, datetime, datetime]:
    name, d = shift_of(now_local, shifts)
    a, b = next((s[1], s[2]) for s in shifts if s[0] == name)
    start = datetime.combine(d, a, tzinfo=now_local.tzinfo)
    end = datetime.combine(d if a < b else d + timedelta(days=1), b, tzinfo=now_local.tzinfo)
    return name, start, end


def fmt_dur(seconds: float | None) -> str:
    if seconds is None or (isinstance(seconds, float) and np.isnan(seconds)):
        return "–"
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_min(minutes: float | None) -> str:
    if minutes is None or (isinstance(minutes, float) and np.isnan(minutes)):
        return "–"
    if minutes >= 120:
        return f"{minutes / 60:.1f} h"
    return f"{minutes:.1f} min" if minutes < 10 else f"{minutes:.0f} min"


# ------------------------------------------------------------------ escalation
def escalation(ct: dict, minutes_open: float) -> tuple[int, str]:
    """Current escalation level (1-3) and the role that should be responding."""
    level, role = 1, ct["l1_role"]
    if ct.get("l2_role") and ct.get("l2_min") is not None and not pd.isna(ct["l2_min"]) and minutes_open >= ct["l2_min"]:
        level, role = 2, ct["l2_role"]
    if ct.get("l3_role") and ct.get("l3_min") is not None and not pd.isna(ct["l3_min"]) and minutes_open >= ct["l3_min"]:
        level, role = 3, ct["l3_role"]
    return level, role


def next_escalation(ct: dict, minutes_open: float) -> tuple[str, float] | None:
    for lvl in (2, 3):
        m, r = ct.get(f"l{lvl}_min"), ct.get(f"l{lvl}_role")
        if r and m is not None and not pd.isna(m) and minutes_open < m:
            return r, m - minutes_open
    return None


def enrich_open(df: pd.DataFrame, cfg: dict, now_utc: datetime) -> pd.DataFrame:
    """Add names, ages, colour state and escalation to open calls."""
    if df.empty:
        return df.assign(age_s=[], state=[], level=[], role=[])
    ct = cfg["call_types"].set_index("id").to_dict("index")
    st = cfg["stations"].set_index("id")["name"].to_dict()
    ln = cfg["lines"].set_index("id")["name"].to_dict()
    out = df.copy()
    out["type"] = out["call_type_id"].map(lambda i: ct.get(i, {}).get("name", "?"))
    out["color"] = out["call_type_id"].map(lambda i: ct.get(i, {}).get("color", "#64748b"))
    out["station"] = out["station_id"].map(st)
    out["line"] = out["line_id"].map(ln)
    out["age_s"] = (pd.Timestamp(now_utc) - out["raised_at"]).dt.total_seconds()
    out["stopped"] = out["stopped_at"].notna()
    out["state"] = np.where(out["stopped"], "red", "yellow")
    levels, roles, nxt = [], [], []
    for _, r in out.iterrows():
        c = ct.get(r["call_type_id"], {"l1_role": "Team leader"})
        # escalation runs until someone acknowledges
        mins = r["age_s"] / 60 if r["status"] == "open" else ((r["ack_at"] - r["raised_at"]).total_seconds() / 60)
        lv, ro = escalation(c, mins)
        levels.append(lv)
        roles.append(ro)
        nxt.append(next_escalation(c, mins) if r["status"] == "open" else None)
    out["level"], out["role"], out["next_esc"] = levels, roles, nxt
    return out.sort_values(["stopped", "level", "age_s"], ascending=[False, False, False])


# ------------------------------------------------------------------ analytics
def enrich_history(df: pd.DataFrame, cfg: dict, zone: ZoneInfo, shifts, now_utc: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    ct = cfg["call_types_all"].set_index("id")
    out = df.copy()
    out["type"] = out["call_type_id"].map(ct["name"])
    out["line"] = out["line_id"].map(cfg["lines_all"].set_index("id")["name"])
    out["station"] = out["station_id"].map(cfg["stations_all"].set_index("id")["name"])
    out["reason"] = out["reason_id"].map(cfg["reasons_all"].set_index("id")["name"]).fillna("(not resolved)")
    end = out["resolved_at"].fillna(pd.Timestamp(now_utc))
    out["mtta_min"] = (out["ack_at"] - out["raised_at"]).dt.total_seconds() / 60
    out["mttr_min"] = (out["resolved_at"] - out["raised_at"]).dt.total_seconds() / 60
    out["downtime_min"] = np.where(out["stopped_at"].notna(), (end - out["stopped_at"]).dt.total_seconds() / 60, 0.0)
    out["line_stop"] = out["stopped_at"].notna()
    local = out["raised_at"].dt.tz_localize(UTC).dt.tz_convert(zone)
    sd = [shift_of(t.to_pydatetime(), shifts) for t in local]
    out["shift"] = [s for s, _ in sd]
    out["prod_date"] = pd.to_datetime([d for _, d in sd])
    out["hour"] = local.dt.hour
    waited = out["mtta_min"].fillna(out["age_s"] / 60 if "age_s" in out else (pd.Timestamp(now_utc) - out["raised_at"]).dt.total_seconds() / 60)
    l2 = out["call_type_id"].map(ct["l2_min"])
    out["escalated"] = waited >= l2.fillna(np.inf)
    return out


def kpis(h: pd.DataFrame) -> dict:
    if h.empty:
        return {"calls": 0, "stops": 0, "downtime": 0.0, "mtta": None, "mttr": None, "escalated": None}
    return {"calls": len(h), "stops": int(h["line_stop"].sum()), "downtime": float(h["downtime_min"].sum()),
            "mtta": float(h["mtta_min"].mean()) if h["mtta_min"].notna().any() else None,
            "mttr": float(h["mttr_min"].mean()) if h["mttr_min"].notna().any() else None,
            "escalated": float(h["escalated"].mean())}
