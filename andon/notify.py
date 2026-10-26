"""Notifications: tell people about line stops and escalations on Microsoft Teams, by SMS or through a webhook.

Each channel (Configure → Notifications) chooses
  trigger  red         a line stops
           escalation  a call is not acknowledged in time and moves to level 2 or 3 (and line stops)
           all         every new call, line stops and escalations
  roles    only calls whose current escalation role is in this list (empty = any role)
  lines    only these lines (empty = all lines)
  kind     log      nothing leaves the app; the message is only written to the notification log (demo, testing)
           teams    Microsoft Teams: a Workflows "When a Teams webhook request is received" URL (Adaptive Card)
           webhook  any HTTPS endpoint (Slack incoming webhook, Power Automate, ServiceNow, PagerDuty adapter…):
                    JSON with "text" plus the call details
           sms      Twilio. target = comma-separated phone numbers (+45…). Needs the secrets
                    TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM.
  target   the URL or phone numbers. Write ${NAME} to keep the real value in Streamlit secrets or an environment variable.

Every message is sent once per call and event (new, red, L2, L3): the log table has a unique key, so two app
instances can never send the same message twice. At most 20 messages per channel per 10 minutes.

Sending runs in the app (or `python -m andon.notify --loop` as a service), never in the shop-floor connector,
so the factory network does not need internet access.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

import pandas as pd
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from . import db, logic

KINDS = {"log": "Log only (nothing is sent)", "teams": "Microsoft Teams", "webhook": "Webhook (JSON)", "sms": "SMS (Twilio)"}
TRIGGERS = {"red": "Line stops", "escalation": "Escalations and line stops", "all": "Every new call, stops and escalations"}
FRESH_S = 600          # only events that happened in the last 10 minutes (no flood after a restart)
MAX_PER_10_MIN = 20
TIMEOUT_S = 8


# ------------------------------------------------------------------ secrets and validation
def secret(name: str) -> str | None:
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st
        return st.secrets.get(name)
    except Exception:
        return None


def resolve_target(target: str | None) -> str:
    """Replace ${NAME} with the secret of that name."""
    return re.sub(r"\$\{([A-Z0-9_]+)\}", lambda m: secret(m.group(1)) or "", target or "").strip()


def mask(target: str | None) -> str:
    """Show a target without leaking a webhook token or full phone numbers."""
    t = target or ""
    if "${" in t:
        return t
    if t.startswith("http"):
        u = urllib.parse.urlsplit(t)
        return f"{u.scheme}://{u.netloc}/…"
    return re.sub(r"\d(?=\d{2})", "•", t)


def check_channel(kind: str, target: str | None) -> str | None:
    """Return a problem with a channel's settings, or None."""
    if kind == "log":
        return None
    real = resolve_target(target)
    if not real:
        return "No target (or the ${SECRET} it refers to is not set)."
    if kind in ("teams", "webhook"):
        if not real.startswith("https://"):
            return "The URL must start with https://"
    if kind == "sms":
        nums = [n.strip() for n in real.split(",") if n.strip()]
        if not nums or not all(re.fullmatch(r"\+\d{7,15}", n) for n in nums):
            return "Phone numbers must look like +4512345678, separated by commas."
        if not (secret("TWILIO_ACCOUNT_SID") and secret("TWILIO_AUTH_TOKEN") and secret("TWILIO_FROM")):
            return "Set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_FROM in the secrets."
    return None


# ------------------------------------------------------------------ messages
def message(call: dict, event: str, app_url: str = "") -> dict:
    icon = {"red": "🔴", "L2": "🟠", "L3": "🚨", "new": "🟡", "test": "🔔"}.get(event, "🔔")
    what = {"red": "Line stopped", "L2": "Escalated to level 2", "L3": "Escalated to level 3", "new": "New call",
            "test": "Test message"}.get(event, event)
    mins = int(call.get("age_s", 0) // 60)
    title = f"{icon} {what}: {call.get('line', '')} · {call.get('station', '')}"
    problem, ctype = call.get("problem") or call.get("type", ""), call.get("type", "")
    body = (f"{problem}{f' ({ctype})' if ctype and ctype != problem else ''}"
            f" · open {mins} min · {'line stopped' if call.get('stopped') else 'line running'}"
            f" · now with {call.get('role', '')}")
    if isinstance(call.get("ack_by"), str) and call["ack_by"]:
        body += f" · acknowledged by {call['ack_by']}"
    link = f"{app_url.rstrip('/')}/responder" if app_url else ""
    return {"title": title, "body": body, "link": link, "text": f"{title}\n{body}" + (f"\n{link}" if link else "")}


def _post_json(url: str, payload: dict) -> str:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "andon-notify"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310 (https enforced in check_channel)
        return f"HTTP {r.status}"


def send(kind: str, target: str | None, msg: dict, call: dict) -> str:
    """Send one message. Returns a short result; raises on failure."""
    if kind == "log":
        return "logged"
    real = resolve_target(target)
    problem = check_channel(kind, target)
    if problem:
        raise ValueError(problem)
    if kind == "teams":
        card = {"type": "AdaptiveCard", "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "version": "1.4",
                "body": [{"type": "TextBlock", "text": msg["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
                         {"type": "TextBlock", "text": msg["body"], "wrap": True}],
                "actions": [{"type": "Action.OpenUrl", "title": "Open in Andon", "url": msg["link"]}] if msg["link"] else []}
        return _post_json(real, {"type": "message", "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": card}]})
    if kind == "webhook":
        return _post_json(real, {"text": msg["text"], "title": msg["title"], "body": msg["body"], "link": msg["link"],
                                 "call": {k: (str(v) if isinstance(v, (datetime, pd.Timestamp)) else v) for k, v in call.items()
                                          if k in ("id", "line", "station", "type", "problem", "stopped", "level", "role", "age_s")}})
    if kind == "sms":
        sid, token, sender = secret("TWILIO_ACCOUNT_SID"), secret("TWILIO_AUTH_TOKEN"), secret("TWILIO_FROM")
        auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
        sent = 0
        for to in [n.strip() for n in real.split(",") if n.strip()]:
            data = urllib.parse.urlencode({"To": to, "From": sender, "Body": msg["text"][:600]}).encode()
            req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data,
                                         method="POST", headers={"Authorization": f"Basic {auth}"})
            with urllib.request.urlopen(req, timeout=TIMEOUT_S):  # noqa: S310
                sent += 1
        return f"{sent} SMS"
    raise ValueError(f"Unknown channel kind {kind}")


# ------------------------------------------------------------------ deciding what to send
def _cfg(engine) -> dict:
    out = {k: db.read_df(engine, t) for k, t in (("call_types", db.call_types), ("stations", db.stations), ("lines", db.lines))}
    ev = db.read_df(engine, db.events)
    out["event_names"] = ev.set_index("id")["short_name"].to_dict() if not ev.empty else {}
    return out


def due_events(call: dict, ct: dict, now: datetime) -> list[str]:
    """Which notification events this open call has reached recently: new, red, L2, L3."""
    out = []
    age = call["age_s"]
    if age <= FRESH_S:
        out.append("new")
    if call.get("stopped") and pd.notna(call.get("stopped_at")) and (now - call["stopped_at"]).total_seconds() <= FRESH_S:
        out.append("red")
        if "new" in out:  # a call raised as a line stop: one message, not two
            out.remove("new")
    if call.get("status") == "open":  # escalation only runs until someone acknowledges
        for lvl in (2, 3):
            m = ct.get(f"l{lvl}_min")
            if ct.get(f"l{lvl}_role") and m is not None and not pd.isna(m) and 0 <= age - float(m) * 60 <= FRESH_S:
                out.append(f"L{lvl}")
    return out


def wanted(channel: dict, event: str, call: dict) -> bool:
    trig = channel.get("trigger") or "red"
    if trig == "red" and event != "red":
        return False
    if trig == "escalation" and event not in ("red", "L2", "L3"):
        return False
    roles = json.loads(channel["roles"]) if isinstance(channel.get("roles"), str) and channel["roles"] else []
    if roles and call.get("role") not in roles:
        return False
    lines = json.loads(channel["lines"]) if isinstance(channel.get("lines"), str) and channel["lines"] else []
    if lines and int(call["line_id"]) not in [int(x) for x in lines]:
        return False
    return True


def check(engine, now: datetime | None = None, app_url: str | None = None) -> int:
    """Send what is due. Returns the number of messages sent or logged."""
    now = now or db.utcnow()
    chans = db.read_df(engine, db.notify_channels, active_only=True)
    if chans.empty:
        return 0
    oc = db.open_calls(engine)
    if oc.empty:
        return 0
    cfg = _cfg(engine)
    oc = logic.enrich_open(oc, cfg, now)
    cts = cfg["call_types"].set_index("id").to_dict("index")
    app_url = app_url if app_url is not None else (db.get_settings(engine).get("app_url") or "")
    done = 0
    for call in oc.to_dict("records"):
        call["problem"] = cfg["event_names"].get(int(call["event_id"]), call["type"]) if pd.notna(call.get("event_id")) else call["type"]
        for event in due_events(call, cts.get(call["call_type_id"], {}), now):
            for ch in chans.to_dict("records"):
                if wanted(ch, event, call):
                    done += _deliver(engine, ch, call, event, app_url, now)
    return done


def _deliver(engine, ch: dict, call: dict, event: str, app_url: str, now: datetime) -> int:
    msg = message(call, event, app_url)
    with engine.begin() as cx:
        recent = cx.execute(select(func.count()).select_from(db.notify_log).where(
            db.notify_log.c.channel_id == ch["id"], db.notify_log.c.ts >= now - timedelta(minutes=10),
            db.notify_log.c.status.in_(["sent", "logged", "sending"]))).scalar() or 0
    status = "throttled" if recent >= MAX_PER_10_MIN else "sending"
    try:  # claim it first: the unique key makes sure only one process ever sends this message
        with engine.begin() as cx:
            lid = cx.execute(insert(db.notify_log).values(ts=now, channel_id=int(ch["id"]), call_id=int(call["id"]), event=event,
                                                          status=status, message=msg["text"][:500])).inserted_primary_key[0]
    except IntegrityError:
        return 0
    if status == "throttled":
        return 0
    try:
        detail = send(ch["kind"], ch.get("target"), msg, call)
        status = "logged" if ch["kind"] == "log" else "sent"
    except Exception as exc:  # never let a notification problem break the boards
        detail, status = f"{type(exc).__name__}: {str(exc)[:200]}", "failed"
    with engine.begin() as cx:
        cx.execute(update(db.notify_log).where(db.notify_log.c.id == lid).values(status=status, detail=detail[:300]))
    return 1 if status in ("sent", "logged") else 0


def check_if_due(engine, every_s: int = 15) -> int:
    """Run check() at most every few seconds, whichever app session gets there first."""
    st = db.get_settings(engine)
    now = db.utcnow()
    last = st.get("notify_last") or "1970-01-01T00:00:00"
    try:
        if (now - datetime.fromisoformat(last)).total_seconds() < every_s:
            return 0
    except ValueError:
        pass
    with engine.begin() as cx:
        n = cx.execute(update(db.settings).where(db.settings.c.key == "notify_last", db.settings.c.value == last)
                       .values(value=now.isoformat())).rowcount
        if not n:
            if cx.execute(select(db.settings.c.key).where(db.settings.c.key == "notify_last")).first():
                return 0
            cx.execute(insert(db.settings).values(key="notify_last", value=now.isoformat()))
    try:
        return check(engine, now)
    except Exception:
        return 0


def send_test(engine, channel: dict, actor: str = "admin", client: str | None = None) -> tuple[bool, str]:
    call = {"id": 0, "line": "Line 1", "station": "Test station", "type": "Maintenance", "problem": "Test message from Andon",
            "stopped": False, "role": "Maintenance", "age_s": 0, "line_id": 0}
    msg = message(call, "test", db.get_settings(engine).get("app_url") or "")
    try:
        detail = send(channel["kind"], channel.get("target"), msg, call)
        ok = True
    except Exception as exc:
        detail, ok = f"{type(exc).__name__}: {str(exc)[:200]}", False
    with engine.begin() as cx:
        cx.execute(insert(db.notify_log).values(ts=db.utcnow(), channel_id=int(channel["id"]), call_id=0,
                                                event=f"test{int(db.utcnow().timestamp()) % 100000}",
                                                status="sent" if ok else "failed", message=msg["text"][:500], detail=detail[:300]))
        db.audit(cx, actor, "notify_test", f"channel {channel['id']} · {'ok' if ok else 'failed'}", client)
    return ok, detail


def main():  # a small service for plants that want notifications without a screen open
    import argparse
    import time
    ap = argparse.ArgumentParser(description="Andon notifications")
    ap.add_argument("--loop", action="store_true", help="keep running, check every 15 s")
    args = ap.parse_args()
    engine = db.make_engine(os.environ.get("DATABASE_URL"))
    while True:
        n = check(engine)
        print(f"{datetime.now():%H:%M:%S} {n} notification(s)")
        if not args.loop:
            break
        time.sleep(15)


if __name__ == "__main__":
    main()
