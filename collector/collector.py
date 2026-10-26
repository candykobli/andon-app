"""Andon connector: reads machine signals on the factory network and writes them to the Andon database.

Sources
  opcua   PLCs and SCADA servers (Ignition, WinCC, Kepware, Siemens, Beckhoff)
  mqtt    IoT gateways, Node-RED, Ignition MQTT Transmission (JSON)
  modbus  older PLCs, meters and simple sensors (Modbus TCP)
  kafka   plant or enterprise event streams (Confluent, Redpanda, Azure Event Hubs, AWS MSK)
  sql     SCADA historians and Ignition transaction groups (SQL Server, PostgreSQL, MySQL, Oracle)
  http    push from any SCADA or script (POST /signals, and POST /batches for MES/ERP batches)
  mes     batches from MES / ERP: a read-only SQL view or a REST endpoint, polled

Security by design
  - Read-only: the connector never writes to a PLC, SCADA or broker. Give it read-only accounts.
  - Outbound only: it opens connections to the machines and to the database; nothing needs to reach it
    from outside, except the optional HTTP push port inside the plant network.
  - Encrypted where the protocol allows it (OPC UA security policies, MQTT/Kafka TLS, PostgreSQL TLS, HTTPS push).
  - Secrets come from environment variables (${VAR} in the config), never from the file itself.
  - Store-and-forward: if the database is unreachable, readings are buffered and sent when it returns.

Run:  python collector/collector.py --config collector/config.yaml
"""
from __future__ import annotations

import argparse
import collections
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import ssl
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from andon import batches as batch_mod  # noqa: E402
from andon import db, oee, signals  # noqa: E402

log = logging.getLogger("andon.collector")
MAX_BODY = 1_000_000          # bytes accepted per HTTP push request
MAX_ITEMS = 5_000             # readings accepted per HTTP push request
MAX_PENDING = 50_000          # readings kept while the database is unreachable


# ------------------------------------------------------------------ helpers
def env(value) -> str:
    """Expand ${VAR} from the environment. Unset variables become empty strings."""
    return os.path.expandvars(str(value)) if value is not None else ""


class StationMap:
    """Resolves 'Line 1/Feed' (or a numeric id) to a station id. Reloads every minute."""

    def __init__(self, engine):
        self.engine = engine
        self.by_name: dict[str, int] = {}
        self.ids: set[int] = set()
        self.loaded = 0.0

    def get(self, ref) -> int | None:
        if time.time() - self.loaded > 60:
            self.reload()
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            return int(ref) if int(ref) in self.ids else None
        return self.by_name.get(_norm(str(ref)))

    def reload(self):
        with self.engine.connect() as cx:
            rows = cx.execute(select(db.stations.c.id, db.stations.c.name, db.lines.c.name.label("line"))
                              .join(db.lines, db.lines.c.id == db.stations.c.line_id)
                              .where(db.stations.c.active.is_(True))).all()
        self.by_name = {_norm(f"{r.line}/{r.name}"): r.id for r in rows}
        self.ids = {r.id for r in rows}
        self.loaded = time.time()


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("\\", "/").strip().lower())


def transform(value, tag: dict):
    """Apply scale/offset for numbers and a value map for states (e.g. {0: Stopped, 1: Running})."""
    if value is None:
        return None
    vmap = tag.get("map")
    if vmap:
        key = value
        if isinstance(value, float) and value.is_integer():
            key = int(value)
        for k in (key, str(key)):
            if k in vmap:
                return vmap[k]
        return tag.get("default", str(value))
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value) * float(tag.get("scale", 1)) + float(tag.get("offset", 0))
    except (TypeError, ValueError):
        return str(value)[:80]


def parse_ts(v, clamp: bool = True) -> datetime | None:
    """Accept ISO strings, epoch seconds or milliseconds and datetimes; return naive UTC."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, datetime):
            dt = v
        elif isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit()):
            x = float(v)
            dt = datetime.fromtimestamp(x / 1000 if x > 1e11 else x, tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return min(dt, db.utcnow()) if clamp else dt  # never store timestamps from the future
    except (ValueError, OverflowError, OSError):
        return None


class Buffer:
    """Thread-safe store of the latest value per (station, key) from push sources (MQTT, Kafka, HTTP)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.items: dict = {}
        self.batches: collections.deque = collections.deque(maxlen=10_000)

    def put_batch(self, rec: dict):
        with self.lock:
            self.batches.append(rec)

    def drain_batches(self) -> list:
        with self.lock:
            out, self.batches = list(self.batches), collections.deque(maxlen=10_000)
        return out

    def put(self, station, key, value, ts=None):
        if station is None or key is None:
            return
        with self.lock:
            self.items[(str(station), str(key))] = (value, ts)

    def drain(self) -> list:
        with self.lock:
            items, self.items = self.items, {}
        return [{"station": s, "key": k, "value": v, "ts": t} for (s, k), (v, t) in items.items()]


def records_from_json(data, fields: dict | None = None, default_station=None, signal_cfg: dict | None = None) -> list[tuple]:
    """Turn a JSON message into (station, key, value, ts) tuples. Accepts:
       {"station": "Line 1/Feed", "signal": "state", "value": "Running", "ts": "..."}
       {"station": "Line 1/Feed", "values": {"state": "Running", "speed": 21.4}, "ts": "..."}
       a list of either, or (with default_station) a plain {"state": ..., "speed": ...} object.
       `fields` renames keys for existing schemas, e.g. {"station": "asset", "signal": "tag", "value": "val"}."""
    f = {"station": "station", "signal": "signal", "value": "value", "values": "values", "ts": "ts", **(fields or {})}
    sc = signal_cfg or {}
    out = []
    items = data if isinstance(data, list) else [data]
    for it in items:
        if not isinstance(it, dict):
            continue
        station = it.get(f["station"], default_station)
        ts = parse_ts(it.get(f["ts"]))
        if isinstance(it.get(f["values"]), dict):
            for k, v in it[f["values"]].items():
                out.append((station, k, transform(v, sc.get(k, {})), ts))
        elif f["signal"] in it or "key" in it:
            k = it.get(f["signal"]) or it.get("key")
            out.append((station, k, transform(it.get(f["value"]), sc.get(k, {})), ts))
        elif default_station is not None:
            for k, v in it.items():
                if k != f["ts"]:
                    out.append((default_station, k, transform(v, sc.get(k, {})), ts))
    return out


# ------------------------------------------------------------------ OPC UA (polling)
class OpcUaSource:
    def __init__(self, cfg: dict):
        from asyncua.sync import Client  # imported only when used
        self.Client = Client
        self.cfg = cfg
        self.client = None
        self.nodes = []
        if not cfg.get("security"):
            log.warning("OPC UA %s has no security policy: traffic is not encrypted. Use 'security: "
                        "Basic256Sha256,SignAndEncrypt,cert.der,key.pem' in production.", cfg.get("endpoint"))

    def _connect(self):
        c = self.Client(self.cfg["endpoint"], timeout=float(self.cfg.get("timeout", 5)))
        if self.cfg.get("username"):
            c.set_user(env(self.cfg["username"]))
            c.set_password(env(self.cfg.get("password", "")))
        if self.cfg.get("security"):
            c.set_security_string(env(self.cfg["security"]))
        c.connect()
        self.client = c
        self.nodes = [(t, c.get_node(t["node"])) for t in self.cfg.get("tags", [])]
        log.info("OPC UA connected to %s (%d tags)", self.cfg["endpoint"], len(self.nodes))

    def read(self) -> list:
        try:
            if self.client is None:
                self._connect()
            values = self.client.read_values([n for _, n in self.nodes])
        except Exception as exc:
            log.warning("OPC UA %s: %s", self.cfg.get("endpoint"), exc)
            self.close()
            return []
        return [{"station": t["station"], "key": t["signal"], "value": transform(v, t)} for (t, _), v in zip(self.nodes, values)]

    def close(self):
        try:
            if self.client:
                self.client.disconnect()
        except Exception:
            pass
        self.client = None


# ------------------------------------------------------------------ Modbus TCP (polling)
class ModbusSource:
    FORMATS = {"int16": (">h", 1), "uint16": (">H", 1), "int32": (">i", 2), "uint32": (">I", 2), "float32": (">f", 2)}

    def __init__(self, cfg: dict):
        from pymodbus.client import ModbusTcpClient
        self.cfg = cfg
        self.client = ModbusTcpClient(cfg["host"], port=int(cfg.get("port", 502)), timeout=float(cfg.get("timeout", 3)))
        self.unit = int(cfg.get("unit", 1))
        log.info("Modbus TCP has no encryption or authentication: keep %s on an isolated control network.", cfg["host"])

    def _call(self, fn, address, count):
        try:
            return fn(address, count=count, device_id=self.unit)   # pymodbus >= 3.10
        except TypeError:
            return fn(address, count=count, slave=self.unit)       # older pymodbus

    def read(self) -> list:
        out = []
        try:
            if not self.client.connected:
                self.client.connect()
            for t in self.cfg.get("tags", []):
                kind = t.get("type", "holding")
                addr = int(t["address"])
                if kind in ("coil", "discrete"):
                    fn = self.client.read_coils if kind == "coil" else self.client.read_discrete_inputs
                    rr = self._call(fn, addr, 1)
                    if rr.isError():
                        raise IOError(rr)
                    raw = bool(rr.bits[0])
                else:
                    fmt, n = self.FORMATS[t.get("datatype", "int16")]
                    fn = self.client.read_holding_registers if kind == "holding" else self.client.read_input_registers
                    rr = self._call(fn, addr, n)
                    if rr.isError():
                        raise IOError(rr)
                    regs = rr.registers if not t.get("swap_words") else list(reversed(rr.registers))
                    raw = struct.unpack(fmt, b"".join(struct.pack(">H", r) for r in regs))[0]
                out.append({"station": t["station"], "key": t["signal"], "value": transform(raw, t)})
        except Exception as exc:
            log.warning("Modbus %s: %s", self.cfg.get("host"), exc)
            try:
                self.client.close()
            except Exception:
                pass
        return out

    def close(self):
        self.client.close()


# ------------------------------------------------------------------ MQTT (push)
class MqttSource:
    """Topic pattern with placeholders, e.g. 'factory/{line}/{station}/{signal}'.
    Payload: a number or text, JSON {"value": ...}, or a JSON object of several signals when the pattern has no {signal}."""

    def __init__(self, cfg: dict, buffer: Buffer):
        import paho.mqtt.client as mqtt
        self.cfg, self.buffer = cfg, buffer
        pattern = cfg.get("topic", "factory/{line}/{station}/{signal}")
        self.sub_topic = re.sub(r"\{[a-z]+\}", "+", pattern)
        self.rx = self.compile(pattern)
        try:
            self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.get("client_id", "andon-collector"))
        except AttributeError:  # paho < 2
            self.client = mqtt.Client(client_id=cfg.get("client_id", "andon-collector"))
        if cfg.get("username"):
            self.client.username_pw_set(env(cfg["username"]), env(cfg.get("password", "")))
        tls = cfg.get("tls")
        if tls:
            opts = tls if isinstance(tls, dict) else {}
            self.client.tls_set(ca_certs=opts.get("ca_file"), certfile=opts.get("cert_file"), keyfile=opts.get("key_file"))
        else:
            log.warning("MQTT %s without TLS: messages travel unencrypted. Set 'tls: true' (port 8883) in production.", cfg["host"])
        self.client.on_connect = lambda c, u, f, rc, *a: (c.subscribe(self.sub_topic), log.info("MQTT subscribed to %s", self.sub_topic))
        self.client.on_message = self.on_message
        self.client.connect_async(cfg["host"], int(cfg.get("port", 8883 if tls else 1883)), keepalive=30)
        self.client.loop_start()

    @staticmethod
    def compile(pattern: str):
        rx = re.escape(pattern)
        for name in ("line", "station", "signal"):
            rx = rx.replace(re.escape("{" + name + "}"), f"(?P<{name}>[^/]+)")
        return re.compile("^" + rx + "$")

    def handle(self, topic: str, payload: bytes):
        m = self.rx.match(topic)
        if not m:
            return
        parts = m.groupdict()
        station = f"{parts['line']}/{parts['station']}" if "line" in parts else parts.get("station")
        text_ = payload[:MAX_BODY].decode("utf-8", "replace").strip()
        try:
            data = json.loads(text_)
        except json.JSONDecodeError:
            data = text_
        sc = self.cfg.get("signals", {})
        if parts.get("signal"):
            if isinstance(data, dict) and "value" in data:
                self.buffer.put(station, parts["signal"], transform(data["value"], sc.get(parts["signal"], {})), parse_ts(data.get("ts")))
            else:
                self.buffer.put(station, parts["signal"], transform(data, sc.get(parts["signal"], {})))
        elif isinstance(data, (dict, list)):
            for st_, k, v, ts in records_from_json(data, self.cfg.get("fields"), station, sc):
                self.buffer.put(st_, k, v, ts)

    def on_message(self, client, userdata, msg):
        try:
            self.handle(msg.topic, msg.payload)
        except Exception as exc:
            log.warning("MQTT message on %s: %s", msg.topic, exc)

    def read(self) -> list:
        return []

    def close(self):
        self.client.loop_stop()
        self.client.disconnect()


# ------------------------------------------------------------------ Kafka (push)
class KafkaSource:
    """Consumes JSON messages from one or more topics. Message shapes: see records_from_json().
    With 'station_from: key' the message key is the station and the value is an object of signals."""

    def __init__(self, cfg: dict, buffer: Buffer):
        from confluent_kafka import Consumer
        self.cfg, self.buffer = cfg, buffer
        conf = {
            "bootstrap.servers": env(cfg["bootstrap_servers"]),
            "group.id": cfg.get("group_id", "andon-collector"),
            "auto.offset.reset": cfg.get("offset_reset", "latest"),   # live data: start from now
            "enable.auto.commit": True,
            "client.id": cfg.get("client_id", "andon-collector"),
        }
        proto = cfg.get("security_protocol", "PLAINTEXT").upper()
        conf["security.protocol"] = proto
        if proto.startswith("SASL"):
            conf["sasl.mechanism"] = cfg.get("sasl_mechanism", "SCRAM-SHA-512")
            conf["sasl.username"] = env(cfg.get("username"))
            conf["sasl.password"] = env(cfg.get("password"))
        if cfg.get("ca_file"):
            conf["ssl.ca.location"] = cfg["ca_file"]
        if cfg.get("cert_file"):
            conf["ssl.certificate.location"] = cfg["cert_file"]
            conf["ssl.key.location"] = cfg["key_file"]
        conf.update({k: env(v) for k, v in (cfg.get("extra") or {}).items()})  # any other librdkafka setting
        if proto in ("PLAINTEXT", "SASL_PLAINTEXT"):
            log.warning("Kafka %s uses %s: data and passwords travel unencrypted. Use SSL or SASL_SSL in production.",
                        conf["bootstrap.servers"], proto)
        self.consumer = Consumer(conf)
        topics = cfg.get("topics") or [cfg.get("topic", "andon.signals")]
        self.consumer.subscribe(topics)
        self.stop = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()
        log.info("Kafka subscribed to %s on %s", ", ".join(topics), conf["bootstrap.servers"])

    def handle(self, key: bytes | None, value: bytes | None):
        if not value:
            return
        data = json.loads(value[:MAX_BODY].decode("utf-8", "replace"))
        default_station = key.decode("utf-8", "replace") if key and self.cfg.get("station_from") == "key" else self.cfg.get("station")
        for st_, k, v, ts in records_from_json(data, self.cfg.get("fields"), default_station, self.cfg.get("signals", {})):
            self.buffer.put(st_, k, v, ts)

    def _loop(self):
        while not self.stop.is_set():
            try:
                msg = self.consumer.poll(1.0)
                if msg is None:
                    continue
                if msg.error():
                    log.warning("Kafka: %s", msg.error())
                    continue
                self.handle(msg.key(), msg.value())
            except Exception as exc:
                log.warning("Kafka message skipped: %s", exc)

    def read(self) -> list:
        return []

    def close(self):
        self.stop.set()
        try:
            self.consumer.close()
        except Exception:
            pass


# ------------------------------------------------------------------ SQL (SCADA historians, transaction groups)
class SqlSource:
    """Polls a read-only SELECT that returns columns station, signal, value (and optionally ts).
    Use :since in the query to read only new rows, e.g.
      SELECT asset AS station, tag AS signal, val AS value, t_stamp AS ts FROM andon_feed WHERE t_stamp > :since"""

    def __init__(self, cfg: dict):
        from sqlalchemy import create_engine
        q = cfg["query"].strip()
        if not re.match(r"(?is)^\s*(select|with)\b", q) or re.search(r"(?i)\b(insert|update|delete|drop|alter|create|merge|exec)\b", q):
            raise ValueError("The sql source only runs read-only SELECT queries.")
        self.cfg, self.query = cfg, text(q)
        self.engine = create_engine(env(cfg["url"]), pool_pre_ping=True)
        self.since = db.utcnow() if cfg.get("start", "now") == "now" else datetime(1970, 1, 1)
        self.uses_since = ":since" in q
        self.every = float(cfg.get("interval_s", 5))
        self.last = 0.0
        log.info("SQL source ready (%s)", self.engine.url.render_as_string(hide_password=True))

    def read(self) -> list:
        if time.time() - self.last < self.every:
            return []
        self.last = time.time()
        try:
            with self.engine.connect() as cx:
                rows = cx.execute(self.query, {"since": self.since} if self.uses_since else {}).mappings().all()
        except Exception as exc:
            log.warning("SQL source: %s", exc)
            return []
        out, sc = [], self.cfg.get("signals", {})
        for r in rows:
            raw = parse_ts(r.get("ts"), clamp=False)   # the source's own clock, for reading only new rows
            if raw and raw > self.since:
                self.since = raw
            ts = parse_ts(r.get("ts"))
            out.append({"station": r["station"], "key": r["signal"], "value": transform(r["value"], sc.get(r["signal"], {})), "ts": ts})
        return out

    def close(self):
        self.engine.dispose()


# ------------------------------------------------------------------ HTTP push (SCADA, scripts)
class HttpSource:
    """POST /signals with JSON (see records_from_json) and header  X-Api-Key: <token>.
    A token is required. Optional: HTTPS (cert_file/key_file) and an allow-list of client addresses."""

    def __init__(self, cfg: dict, buffer: Buffer):
        token = env(cfg.get("token"))
        if not token and not cfg.get("allow_without_token"):
            raise ValueError("The http source needs a token (set token: ${ANDON_PUSH_TOKEN}).")
        if token and len(token) < 16:
            log.warning("The HTTP push token is short; use at least 16 random characters.")
        nets = [ipaddress.ip_network(n, strict=False) for n in cfg.get("allowed_ips", [])]
        buf, fields, sc = buffer, cfg.get("fields"), cfg.get("signals", {})
        batch_fields = cfg.get("batch_fields")

        class Handler(BaseHTTPRequestHandler):
            server_version = "andon-collector"
            sys_version = ""

            def _reply(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _allowed(self) -> bool:
                if not nets:
                    return True
                ip = ipaddress.ip_address(self.client_address[0])
                return any(ip in n for n in nets)

            def do_GET(self):
                if not self._allowed():
                    return self._reply(403, {"error": "address not allowed"})
                self._reply(200, {"status": "ok"}) if self.path.startswith("/health") else self._reply(404, {"error": "not found"})

            def do_POST(self):
                if not self._allowed():
                    log.warning("HTTP push refused from %s (not in allowed_ips)", self.client_address[0])
                    return self._reply(403, {"error": "address not allowed"})
                if not (self.path.startswith("/signals") or self.path.startswith("/batches")):
                    return self._reply(404, {"error": "not found"})
                if token and not hmac.compare_digest(self.headers.get("X-Api-Key", ""), token):
                    log.warning("HTTP push with a wrong token from %s", self.client_address[0])
                    return self._reply(401, {"error": "invalid X-Api-Key"})
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._reply(413, {"error": "request too large"})
                try:
                    data = json.loads(self.rfile.read(length) or b"null")
                    if self.path.startswith("/batches"):
                        rows = data if isinstance(data, list) else [data]
                        rows = [batch_mod.normalise(r, batch_fields) for r in rows[:MAX_ITEMS] if isinstance(r, dict)]
                        for r in rows:
                            buf.put_batch(r)
                        return self._reply(200, {"accepted": len(rows)})
                    recs = records_from_json(data, fields, None, sc)[:MAX_ITEMS]
                    for st_, k, v, ts in recs:
                        buf.put(st_, k, v, ts)
                    self._reply(200, {"accepted": len(recs)})
                except Exception as exc:
                    self._reply(400, {"error": str(exc)[:200]})

            def log_message(self, fmt, *args):
                log.debug(fmt, *args)

        self.server = ThreadingHTTPServer((cfg.get("bind", "0.0.0.0"), int(cfg.get("port", 8088))), Handler)
        scheme = "http"
        if cfg.get("cert_file"):
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(cfg["cert_file"], cfg.get("key_file"))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            scheme = "https"
        else:
            log.warning("HTTP push without TLS: add cert_file/key_file so tokens and data are encrypted.")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info("Push endpoint listening on %s://%s:%s/signals", scheme, cfg.get("bind", "0.0.0.0"), cfg.get("port", 8088))

    def read(self) -> list:
        return []

    def close(self):
        self.server.shutdown()


# ------------------------------------------------------------------ MES / ERP batches
class MesSource:
    """Polls batches (which product and batch each line runs) from MES or ERP. Read-only. Either
      query: a SELECT returning line, batch_no, product, destination, planned_qty, status, started_at, ended_at
             (use :since to read only changed rows), on a read-only database account; or
      url:   an HTTPS endpoint returning a JSON list of batch records (or {"value": [...]} as OData/SAP returns),
             with an optional header such as Authorization: Bearer ${MES_TOKEN}.
    `fields` renames their column names to ours, e.g. {batch_no: ORDER_NUMBER, product: MATERIAL_TEXT}."""

    def __init__(self, cfg: dict):
        self.cfg, self.fields = cfg, cfg.get("fields") or {}
        self.every = float(cfg.get("interval_s", 30))
        self.last = 0.0
        self.since = datetime(1970, 1, 1) if cfg.get("start") == "all" else db.utcnow()
        if cfg.get("query"):
            from sqlalchemy import create_engine
            q = cfg["query"].strip()
            if not re.match(r"(?is)^\s*(select|with)\b", q) or re.search(r"(?i)\b(insert|update|delete|drop|alter|create|merge|exec)\b", q):
                raise ValueError("The mes source only runs read-only SELECT queries.")
            self.query, self.uses_since = text(q), ":since" in q
            self.engine = create_engine(env(cfg["db_url"]), pool_pre_ping=True)
            self.since = datetime(1970, 1, 1) if cfg.get("start", "all") == "all" else self.since
            log.info("MES source ready (SQL, %s)", self.engine.url.render_as_string(hide_password=True))
        elif cfg.get("url"):
            self.url = env(cfg["url"])
            if not self.url.startswith("https://") and not cfg.get("allow_http"):
                raise ValueError("The mes url must use https:// (or set allow_http: true inside an isolated network).")
            self.headers = {k: env(v) for k, v in (cfg.get("headers") or {}).items()}
            self.engine = None
            log.info("MES source ready (REST %s)", self.url.split("?")[0])
        else:
            raise ValueError("The mes source needs either query + db_url, or url.")

    def read(self) -> list:
        return []

    def read_batches(self) -> list:
        if time.time() - self.last < self.every:
            return []
        self.last = time.time()
        try:
            if self.engine is not None:
                with self.engine.connect() as cx:
                    rows = [dict(r) for r in cx.execute(self.query, {"since": self.since} if self.uses_since else {}).mappings()]
            else:
                import urllib.request
                req = urllib.request.Request(self.url, headers={"Accept": "application/json", **self.headers})
                with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
                    data = json.loads(r.read() or b"[]")
                rows = data.get("value", data.get("batches", [])) if isinstance(data, dict) else data
        except Exception as exc:
            log.warning("MES source: %s", exc)
            return []
        out = []
        for r in rows if isinstance(rows, list) else []:
            n = batch_mod.normalise(r, self.fields)
            for k in ("started_at", "ended_at"):
                t = parse_ts(n.get(k), clamp=False) if n.get(k) else None
                if t and t > self.since:
                    self.since = t
            out.append(n)
        return out

    def close(self):
        if self.engine is not None:
            self.engine.dispose()


# ------------------------------------------------------------------ main loop
SOURCES = {"opcua": OpcUaSource, "modbus": ModbusSource, "sql": SqlSource, "mes": MesSource}
PUSH_SOURCES = {"mqtt": MqttSource, "kafka": KafkaSource, "http": HttpSource}


def build_sources(cfg: dict, buffer: Buffer) -> list:
    out = []
    for s in cfg.get("sources", []):
        kind = s.get("type")
        try:
            if kind in SOURCES:
                out.append(SOURCES[kind](s))
            elif kind in PUSH_SOURCES:
                out.append(PUSH_SOURCES[kind](s, buffer))
            else:
                log.error("Unknown source type %r", kind)
        except ImportError as exc:
            log.error("Source %s needs an extra package: %s (pip install -r collector/requirements.txt)", kind, exc)
        except Exception as exc:
            log.error("Source %s not started: %s", kind, exc)
    return out


def run(cfg: dict, once: bool = False) -> None:
    url = env(cfg.get("database_url") or os.environ.get("DATABASE_URL", ""))
    engine = db.make_engine(url or None)
    db.init_db(engine, seed_demo=False)
    if cfg.get("disable_simulator", True):
        db.set_setting(engine, "simulator", "0")  # real data now: stop the in-app simulator
    stations = StationMap(engine)
    buffer = Buffer()
    sources = build_sources(cfg, buffer)
    interval = float(cfg.get("interval_s", 2))
    pending: collections.deque = collections.deque(maxlen=MAX_PENDING)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: stop.set())
    signal.signal(signal.SIGTERM, lambda *a: stop.set())
    log.info("Collector running with %d source(s), every %.1f s", len(sources), interval)
    unknown: set = set()
    pending_batches: collections.deque = collections.deque(maxlen=10_000)
    while not stop.is_set():
        t0 = time.time()
        now = db.utcnow()
        items = buffer.drain()
        for s in sources:
            items.extend(s.read())
        try:
            for it in items:
                sid = stations.get(it["station"])
                if sid is None:
                    if it["station"] not in unknown:
                        unknown.add(it["station"])
                        log.warning("Unknown station %r. Use 'Line name/Station name' exactly as in the app, or the station id.", it["station"])
                    continue
                if it["value"] is None or not str(it["key"]).strip():
                    continue
                pending.append({"station_id": sid, "key": str(it["key"])[:40], "value": it["value"], "ts": it.get("ts") or now})
        except Exception as exc:  # database unreachable while resolving names: keep the raw items for the next round
            log.warning("Station lookup failed: %s", exc)
        if pending:
            batch = list(pending)
            try:
                fired = signals.ingest(engine, batch, now)
                pending.clear()
                log.info("Stored %d readings%s", len(batch), f", raised {fired} auto-call(s)" if fired else "")
            except Exception as exc:
                log.error("Database write failed, keeping %d readings for retry: %s", len(pending), exc)
        try:  # hourly OEE figures, at most once a minute, so OEE stays current even when no screen is open
            oee.rollup_if_due(engine)
        except Exception as exc:
            log.warning("OEE roll-up skipped: %s", exc)
        pending_batches.extend(buffer.drain_batches())
        for s in sources:
            if hasattr(s, "read_batches"):
                pending_batches.extend(s.read_batches())
        if pending_batches:
            recs = list(pending_batches)
            try:
                res = batch_mod.ingest(engine, recs, source="mes")
                pending_batches.clear()
                log.info("Batches: %d started, %d updated, %d ended, %d skipped", res["started"], res["updated"], res["ended"], res["skipped"])
                for ln in sorted(set(res["unknown_lines"])):
                    log.warning("Batch for unknown line %r. Use the line name exactly as in the app, or its id.", ln)
            except Exception as exc:
                log.error("Batch write failed, keeping %d records for retry: %s", len(recs), exc)
        if once:
            break
        stop.wait(max(0.2, interval - (time.time() - t0)))
    for s in sources:
        try:
            s.close()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description="Andon shop-floor connector")
    ap.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    ap.add_argument("--once", action="store_true", help="read once and exit (for testing)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    run(cfg, once=args.once)


if __name__ == "__main__":
    main()
