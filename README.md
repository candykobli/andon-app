# Andon

Andon is a shop-floor call system with live machine signals and an OEE board. **Read [SECURITY.md](SECURITY.md) before connecting it to a real plant.**

- Operators raise a call in two taps, or report the exact problem.
- Responders acknowledge and resolve calls from their phone.
- A wall screen shows every unit in green, yellow or red. Each tile names the exact problem, where it is, and how long it has lasted.
- Machine signals from SCADA, OPC UA, Kafka, MQTT, Modbus or a historian database can raise calls automatically.
- An OEE board shows OEE, TCU and OEE1–3 against target, the loss waterfall, top stops and the running batch, with a different level of detail for each role.
- A chime on the board for new calls, line stops and escalations; Teams, SMS or webhook messages to the right people; batches straight from MES/ERP; a shift report as PDF.

The app is built in Python with Streamlit, SQLAlchemy and Plotly. It runs on any PostgreSQL database, or on a local SQLite file for quick tests.

## Screens and who sees them

Pages a person is not allowed to use are not shown in their menu, and their addresses do not open for them.

| Screen | Address | Who | What |
|---|---|---|---|
| **Operator** | `/?station=ID` | Everyone (station tablets) | *Line running* / *line stopped*, then Maintenance, Quality, Material or Supervisor/help. Optional: pick the exact problem. Start and end planned stops (changeover, cleaning). Shows the running batch, open calls, escalation and live machine data. |
| **Andon board** | `/board` | Line screens: everyone. Management overview: line managers, viewers, engineers, admins | Every unit of a line (line screen) or of several lines (overview): running, problem, line stopped, planned stop, not scheduled, each with its own word, icon and colour. Each tile shows the event name, process, location and a timer that runs until the problem is fixed. Also a strip of open calls and a "recently fixed" strip with durations. |
| **OEE board** | `/oee` | Everyone, with detail by level | See [OEE views](#oee-board). |
| **Responder** | `/responder` | Responders and up | Call queue for your role; acknowledge and resolve with a reason code and note. |
| **Machines** | `/machines` | Responders and up | Live state, speed, counts, process values with limits, OEE per unit, trends. |
| **Analytics** | `/analytics` | Supervisors and up | Downtime, MTTA, MTTR, Pareto, **top stops**, hotspots, shift comparison, CSV export. |
| **Configure** | `/configure` | Process engineer / BA / IT, admin | Boards, lines and units, **event catalogue**, call types and escalation, reason codes, signals and rules, OEE model and targets, shifts and schedule. |
| **Admin** | `/admin` | Admin | **Access requests**, users and PINs, security status and audit log, settings, data tools. |
| **Account** | `/account` | Everyone | Sign in (PIN or company sign-in), sign out, **request access**. |

### Access levels

| Level | Typical people | Gets |
|---|---|---|
| Operator | Line operators | Operator, Andon board, OEE board (operator view) |
| Responder | Maintenance, quality, logistics | + Responder, Machines |
| Supervisor | Team leaders, shift supervisors | + Analytics, OEE supervisor view, batches, handover notes |
| Line manager | Line or area managers | + OEE full view |
| Viewer | Management, QA (read-only) | OEE full view, Machines, Analytics; cannot raise, respond or change anything |
| Process engineer / BA / IT | Configuration owners | + Configure |
| Admin | System owner | + Admin |

**Access is requested, never self-granted.** On *Account → Request access* a person enters their name, work email, the level and lines they need, a reason and a PIN. An admin approves it (and can grant a different level) or rejects it under *Admin → Access requests*. Both are written to the audit log. The form never reveals whether a PIN is already used; the admin sees a warning instead.

**Demo PINs:** Operator 1234 · Supervisor 1111 (Maria) or 5555 (Peter) · Responders 2222, 3333, 4444 · Line manager 6666 · Process engineer 7777 · Viewer 8888 · Admin 0000. One access request (Nadia Rahman) is waiting for approval. Replace the PINs and switch off **Demo mode** before real use.

### TV screens

Wall screens need no sign-in. They show only the boards, with no menu and nothing to click:

- Andon board: `/board?kiosk=1&b=<board id>`
- OEE board: `/oee?kiosk=1&lines=1,2`

Add `&key=<DEVICE_KEY>` when a device key is set.

### One screen per line, one overview for management

Boards are set up under *Configure → Boards* (process engineers, BAs, IT, admins). There are two types:

- **Line screen**: exactly one line, with bigger tiles and a banner when the line stops. This is what the screen mounted at a line shows, so operators only see their own line.
- **Management overview**: several or all lines (remove a line with its ×). Offered only to line managers, viewers (management), engineers and admins when they browse the app. It can still be put on a TV in an office or meeting room through its TV address.

Per board you also choose the tile label (the exact problem, the call type or both), language (English or Danish), theme, which strips and KPIs to show, and sound. Boards can be deleted (the TV address then stops working). The demo has *Line 1 screen*, *Line 2 screen*, a Danish *Line 3 skærm* and a *Management overview*. Upgrading from an older version keeps existing boards and makes a board with exactly one line a line screen.

### Readable for colour-blind people

About 1 in 12 men has a colour vision deficiency, most often red-green. The boards never rely on colour alone:

- Every status has its own **word and icon**: ● Running, ▲ Problem, ■ Line stopped, ‖ Planned or waiting.
- A line stop is **striped**, a problem is bright yellow with dark text, and running units are calm white tiles. That difference also shows in black and white.
- The **light theme** is the default (a dark theme is available per board). Colours come from the Okabe-Ito palette, which stays distinguishable with red-green colour blindness.
- On the OEE board, values against target carry **▲ / ▼ arrows** and the words *on target / below target*. Hourly bars below target are striped, near target dotted.
- Escalated calls pulse unless the screen asks for reduced motion.

This was checked by simulating deuteranopia and protanopia on screenshots of the boards.

**Sound:** the board chimes on a new call (one tone), a line stop (two-tone, repeated) and an escalation (three short beeps). Browsers only allow sound after someone clicks, so click **Enable sound** once under the board, or start the TV's browser in kiosk mode with sound allowed, e.g. `chrome --kiosk --autoplay-policy=no-user-gesture-required "https://andon…/board?kiosk=1&b=2"`. Sound can be switched off per board in *Configure → Boards*.

## Event catalogue: the exact problem, in words operators understand

Every event from every source lands in one catalogue: machine fault codes, SCADA and MES alarms, process alarms (filling, mixing, packaging…), rule alerts and operator calls.

- **New events appear automatically** the first time a source sends them. The app gives them a short default name (max 26 characters) and guesses the call type, whether it stops the line and its OEE category from keywords.
- **Process engineers, BAs and IT rename them** in *Configure → Event catalogue*, in English and Danish, for example `E-620 Vision reject limit` → *Too many camera rejects*. The board shows the new name; the original code and text are kept and shown to engineers. Every rename is in the audit log. Use *Only not yet reviewed* to find new entries.
- Per event you also set the call type, line stop (red) or not (yellow), priority, **Call someone** (show it on the board and raise a call, or just record it), and the **OEE category**:
  - **unplanned**: counts against the line (breakdowns, jams)
  - **planned**: changeover, cleaning, planned maintenance; shown blue
  - **external**: no material, no staff, utilities; excluded from OEE1
- Every occurrence is logged with start and end, so each stop has a duration from the moment it starts until it is fixed. That feeds **top stops** on the OEE board and in Analytics.

## OEE board

### The time model (defaults, editable under *Configure → OEE model & targets*)

```
Calendar time
 − not scheduled              = scheduled time
 − planned stops              = planned production time (PPT)
 − external stops             = line-controllable time (LCT)
 − unplanned stops            = operating time
 − microstops (< 2 min) and speed loss  = net operating time
 − quality loss (scrap)       = valued operating time  (good units ÷ ideal rate)

TCU  = valued ÷ calendar        OEE3 = valued ÷ scheduled
OEE2 = valued ÷ PPT             OEE1 = valued ÷ LCT
OEE  = availability × performance × quality, on PPT (= OEE2) or on LCT (= OEE1)
```

The microstop threshold, the base of the headline OEE and the targets per line are settings. Schedules (which shifts run on which line, weekends) come from *Configure → Shifts & schedule*.

### Views

| View | Who | Shows |
|---|---|---|
| Operator (also the TV) | Everyone | OEE vs target, availability, performance, quality, good output, scrap, running and last batch (product, batch number, destination), top 3 stops this shift, OEE per hour, latest handover note |
| Supervisor | Supervisors | + every machine with its state and OEE, top stops with durations, OEE per shift over 7 days, start/end a batch, write a handover note |
| Full | Line managers, engineers, viewers, admins | + OEE, TCU, OEE3, OEE2, OEE1, A, P, Q actual vs target per line, the waterfall from total calendar time to valued operating time, OEE per machine, OEE per hour over 24 h, CSV export |

Periods: this shift, last shift, today, last 24 h, last 7 or 14 days.

### Batches from MES / ERP

The running batch (product, batch number, destination, planned quantity) reaches the OEE board in one of four ways, each shown with its source:

1. **`mes` connector source**: polls a read-only SQL view of the MES (Siemens Opcenter, Werum PAS-X, Rockwell, a custom table) or a REST/OData endpoint (e.g. SAP S/4HANA production orders). Field names are mapped in the config.
2. **HTTPS push**: MES or an integration platform posts to the connector's `/batches` endpoint.
3. **Signal keys** `batch_no`, `product`, `destination`, `planned_qty` on the line's output unit (simple PLC or SCADA tags).
4. **By hand**: a supervisor starts and ends a batch on the OEE board.

A new batch number on a line closes the previous batch. A completed order ends it. An old batch number that is sent again is not reopened.

### Important: informational only

OEE figures and batch information on this board are **informational**. They are not a batch record and do not replace MES, ERP or the electronic batch record. In a GMP plant, do not use them for release decisions.

## Notifications: Teams, SMS, webhook

*Configure → Notifications* (engineers and admins). Each channel sets:

| Setting | Options |
|---|---|
| **Send by** | `teams` (a Teams Workflows webhook: "When a Teams webhook request is received", posts an Adaptive Card with an *Open in Andon* button), `sms` (Twilio), `webhook` (any HTTPS endpoint: Slack, Power Automate, ServiceNow, a PagerDuty adapter…), `log` (nothing leaves the app; used in the demo) |
| **When** | line stops · escalations and line stops · every new call, stops and escalations |
| **Roles / lines** | only calls now with these escalation roles (e.g. Supervisor, Production manager), only these lines |
| **Target** | the webhook URL or phone numbers, or `${SECRET_NAME}` so the real value stays in the hosting platform's secrets |

Example message: *🟠 Escalated to level 2: Line 1 · Assembly B. Gripper not closing (Maintenance) · open 6 min · line stopped · now with Supervisor.*

- Each message is sent **once** per call and event (new, line stop, level 2, level 3), even with several app instances.
- At most 20 messages per channel per 10 minutes, so a bad day doesn't become a message storm.
- **Send test** checks a channel; every message and its result is listed under *Recent notifications*.
- SMS needs `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN` and `TWILIO_FROM` in the secrets.
- Notifications are sent **by the app**, never by the shop-floor connector, so the plant network needs no internet access. They run while any board is open; for plants that want them without a screen, run `python -m andon.notify --loop` as a small service next to the app.

## Shift report (PDF)

Supervisors and up: *OEE board → Shift report*. Pick lines, a production date and a shift (the last finished shift is preselected). The PDF has a summary across lines, then per line: OEE, availability, performance, quality, OEE1–3 and TCU against target; where the time went; OEE per hour; top stops; every call with response and fix time, reason and who fixed it; batches; handover notes. Creating a report is recorded in the audit log.

## How a call works

1. **Raised.** An operator taps a call type (optionally the exact problem), or a machine event or rule fires. The call is yellow, or red if the line is stopped.
2. **Escalated.** If nobody acknowledges it, it moves up the chain set for its call type, for example Maintenance → Supervisor after 5 min → Production manager after 15 min.
3. **Acknowledged.** A responder confirms with a PIN; the station and board show who is coming.
4. **Resolved.** The responder picks a reason code and can add a note. The timer stops and the duration is stored.

Operators can cancel a call raised by mistake within 2 minutes, as long as nobody has acknowledged it.

## Machine signals and automatic calls

```
 PLCs / sensors / SCADA ──► connector (factory PC) ──► PostgreSQL ◄── Andon app (boards, tablets, phones)
   OPC UA · MQTT · Modbus · Kafka · SQL · HTTPS   events, rules, OEE roll-up      refreshes every 5–30 s
```

- **Signal keys per unit** (*Configure → Signals & rules*):
  - `state`: Running / Idle / Stopped / Fault / Planned / Off
  - `fault_code`: machine alarm code or text, matched to the event catalogue
  - `event`: process or MES alarm text, matched to the event catalogue
  - `speed` with a target; `good` and `scrap` counters
  - `batch_no`, `product`, `destination`: the running batch
  - any process value (temperature, pressure, vibration…) with limits
- **Rules** turn signals into calls, for example: fault for 30 s → red Maintenance call; idle for 2 min → Material call; temperature out of limits for 60 s → Quality call; any process alarm → the call type set on the event. A rule fires once per occurrence and never duplicates an open call.
- **Simulator:** without real machines, the built-in simulator follows the line schedules, runs batches with changeovers, and generates faults, starvation, slow spells and process alarms, so the demo always looks live. In demo mode, gaps in OEE history are filled with synthetic data and a "Demo bot" closes forgotten calls.

### The connector (`collector/`)

The connector runs on a PC or server that can reach both the data sources and the database. It reads every few seconds, writes the values, tracks events and batches, checks the rules and rolls up OEE every minute.

```bash
pip install -r collector/requirements.txt
cp collector/config.example.yaml collector/config.yaml     # then edit
export DATABASE_URL="postgresql://..."                      # same database as the app
python collector/collector.py --config collector/config.yaml
```

| Source | Typical use | Config |
|---|---|---|
| `opcua` | SCADA servers (Ignition, WinCC OA, Kepware), PLCs (S7-1500, Beckhoff, B&R) | Endpoint, security, user/password, node ids mapped to station + signal |
| `mqtt` | IoT sensors, Node-RED, Ignition MQTT in JSON mode | Broker, topic pattern such as `factory/{line}/{station}/{signal}`, value maps |
| `modbus` | Older PLCs, energy meters, simple sensors | Host, unit id, registers with data type and scale |
| `kafka` | Plant or enterprise event streams (Confluent, Redpanda, MSK, Event Hubs) | Brokers, topics, consumer group, `SASL_SSL`/`SSL`, JSON field mapping |
| `sql` | SCADA historians, Ignition transaction groups, MES tables | Connection URL and a read-only `SELECT … WHERE ts > :since` |
| `mes` | MES / ERP batches | Read-only SQL view with `:since`, or HTTPS REST/OData URL with a token header; field mapping |
| `http` | Any SCADA or script that can POST | Port, required token, HTTPS, IP allow-list. POST `/signals` with `{"station": "Line 2/Test", "values": {"state": "Running", "event": "Filler low level"}}` |

- **Tag mapping:** units are referenced as `Line name/Unit name`, exactly as in the app. Numeric PLC states are translated with `map: {0: Stopped, 1: Running, 2: Idle, 3: Fault}`; raw values can be adjusted with `scale` and `offset`.
- **Sparkplug B:** binary payloads. Let Ignition or Node-RED republish them as JSON, or read the same tags over OPC UA.
- **Running it permanently:** a Windows service (NSSM, Task Scheduler), systemd, or the Docker image.
- **Network:** outbound only to the database port (5432, or 6543 for the Supabase pooler).
- When the connector starts, it switches off the in-app simulator.

## Choosing a database

Set `DATABASE_URL` in Streamlit Secrets or as an environment variable. Any PostgreSQL works:

| Host | Good for | Notes |
|---|---|---|
| **Supabase** (free tier) | Demos and pilots | Project → **Connect** → **Session pooler** URI (the direct connection is IPv6-only, which Streamlit Cloud cannot reach). |
| **Neon** (free tier) | Demos and pilots | Connection string from the dashboard; scales to zero when idle. |
| **Azure Database for PostgreSQL** | Microsoft-based companies | Private networking and Entra ID. |
| **AWS RDS / Google Cloud SQL** | AWS or GCP companies | Managed backups and high availability. |
| **On-premise PostgreSQL** | Plants that keep data on site | Run the app and connector inside the factory network with Docker. |

Without `DATABASE_URL`, the app uses a local `andon.db` SQLite file. That's fine on your computer, but on Streamlit Community Cloud the file is wiped when the app restarts.

Tables are created automatically on first start (all prefixed `andon_`). **Upgrading from the first version** is automatic too: new tables and columns are added, existing data is kept, and a default board and planned-stop reasons are created. Set `SEED_DEMO = "0"` to start empty.

## Run it

**Locally:**
```bash
pip install -r requirements.txt
streamlit run app.py          # http://localhost:8501 with the demo plant
```

**Streamlit Community Cloud (free):**
1. Push this folder to GitHub (every `__init__.py` contains a comment, so the web uploader keeps it).
2. On share.streamlit.io choose **Create app**, main file `app.py`.
3. Under **Advanced settings → Secrets**, add `DATABASE_URL = "postgresql://..."`.

**Docker (company server, Azure Container Apps, AWS):**
```bash
docker build -t andon .
docker run -p 8501:8501 -e DATABASE_URL="postgresql://..." andon
```

## Security

[SECURITY.md](SECURITY.md) has the network design, the built-in controls and the go-live checklist. In short:

- **The connector is read-only and outbound-only.** It never sends commands to machines and should sit in the industrial DMZ, reading from SCADA, the historian, Kafka or MQTT rather than from PLCs.
- **Access levels with an approval flow**, PINs with lock-out, optional company sign-in, device keys for tablets and TVs.
- **Append-only audit log** of calls, renames, configuration changes, approvals, sign-ins and wrong PINs.
- **Least-privilege database accounts** from `sql/roles.sql`.
- **Hosting:** the public Streamlit Cloud link is for the simulated demo only.

## Project layout

```
app.py                  navigation; menus by access level; kiosk mode for TVs
andon/db.py             tables, upgrades, call lifecycle, users and access requests, audit log
andon/events.py         event catalogue: default names, classification, occurrences with durations
andon/oee.py            schedules, hourly OEE roll-up, OEE/TCU/OEE1-3, waterfall, targets, demo history
andon/signals.py        signal ingest, event and batch tracking, rules engine, simulator
andon/logic.py          shifts, escalation, call KPIs (MTTA, MTTR, downtime)
andon/notify.py         Teams / SMS / webhook notifications, once per call and event, with throttling
andon/batches.py        batches from MES / ERP (SQL view, REST, push)
andon/report.py         shift report PDF (ReportLab)
andon/demo.py           demo plant: 3 lines, users, catalogue (EN/DA), boards, notification channels, history
andon/ui.py             database connection, cached configuration, sign-in, permissions, styling
views/                  operator, board, oee_board, responder, machines, analytics, configure, admin, account
collector/              shop-floor connector (OPC UA, MQTT, Modbus TCP, Kafka, SQL, HTTPS push, MES/ERP batches)
sql/roles.sql           least-privilege database accounts for the app and the connector
SECURITY.md             network architecture, built-in controls and go-live checklist
```
