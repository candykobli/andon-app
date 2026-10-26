# Security

An Andon system touches the shop floor, so the first rule is that it must never become a way into the machines. This document explains how the app and connector are built to avoid that. It also lists what the plant's IT/OT team still has to provide, and the checklist to complete before connecting to a real plant.

## What we protect, and from what

| Risk | Example | Main protection |
|---|---|---|
| **A path into the control network** (the most serious risk) | Someone uses the app or connector to reach PLCs | The connector is read-only and outbound-only, sits in a DMZ, and nothing from outside connects into the plant network |
| **False or closed calls** | Someone fakes a line stop or closes a call without fixing it | Device keys for tablets, PINs with lock-out, company sign-in, audit log |
| **Unauthorised configuration** | Someone renames an event, changes OEE targets or adds units | Access levels, admin-approved access requests, audit log of every change |
| **Misused figures** | OEE or batch data from this app treated as a quality record | Labelled informational only; MES / eBR stay the system of record |
| **Leak of production data** | Output, scrap and downtime figures are commercially sensitive | TLS everywhere, private hosting, least-privilege database accounts |
| **Outage** | The database or network goes down | Store-and-forward in the connector, managed database with backups |

## Recommended network architecture

This follows the zones-and-conduits idea from IEC 62443 and the Purdue model:

```
 Level 4  Enterprise / cloud       Andon app (company sign-in) ── PostgreSQL (TLS, backups)
                                              ▲  HTTPS                  ▲ TLS 5432/6543
 ─────────── firewall ────────────────────────┼─────────────────────────┼──────────────────
 Level 3.5 Industrial DMZ                     │               Andon connector
                                              │      (read-only accounts, outbound only)
 ─────────── firewall ──────────────────────────────────────────────────▲──────────────────
 Level 3   Site operations         SCADA / historian / MES / Kafka / MQTT broker
 Level 0–2 Control                 PLCs, sensors, HMIs     (the connector does not talk to them directly)
```

- **Read from Level 3, not from PLCs.** Where possible, read from the SCADA server (OPC UA), the historian (SQL source), the plant Kafka cluster or the MQTT broker. Those systems already collect the PLC data. Reading from them keeps the connector out of the control network and adds no load on the PLCs. Poll PLCs directly (OPC UA or Modbus) only on an isolated machine network with the OT team's approval.
- **Outbound only.** The connector opens every connection itself: to SCADA, Kafka or the broker, and to the database. No firewall rule lets anything from Level 4 or the internet connect into Level 3 or below. The only listening port is the optional HTTP push endpoint, and it should only be reachable from inside the plant network (use `allowed_ips`).
- **Kafka or MQTT in the DMZ** is the cleanest pattern when it exists. The OT side publishes out to the broker, and the connector subscribes. The two networks never connect directly.
- **The app stays off the public internet** once it holds real data. Run it inside the company network, on Azure or AWS with private access, or as a private Streamlit app limited to named users. Streamlit Community Cloud with a public link is fine for the demo with simulated data only.

## What is built in

**Connector (`collector/`)**
- **Read-only.** There is no code path that writes to a PLC, SCADA system, historian, broker or Kafka topic. The SQL source refuses anything other than a `SELECT`.
- **Encryption** wherever the protocol allows it, with a warning in the log when it is off:
  - OPC UA security policies (Basic256Sha256, Sign & Encrypt, with certificates)
  - MQTT over TLS, optionally with client certificates
  - Kafka with `SSL` or `SASL_SSL` (SCRAM)
  - HTTPS for the push endpoint
  - PostgreSQL TLS, switched on automatically for every database that is not on the same machine
- **HTTP push hardening.** The endpoint will not start without a token. It also enforces:
  - constant-time token comparison
  - an optional IP allow-list
  - a 1 MB size limit and a cap on readings per request
  - no stack traces in replies
- **Secrets only from environment variables** (`${VAR}` in the config file). `collector/config.yaml` is in `.gitignore`.
- **Store-and-forward.** If the database is unreachable, up to 50,000 readings are kept in memory with their original timestamps and written as soon as it returns. Readings with timestamps in the future are clamped to the present.
- **Unknown stations are ignored and logged.** A source cannot create stations or rules.

**App**
- **Access levels.** Operator, responder, supervisor, line manager, viewer (read-only), process engineer / BA / IT, and admin. Each page checks the level on the server; pages a person may not use are not even registered in their session, so typing the address does not open them. Configuration (boards, units, event names, rules, OEE model) is limited to process engineers, BAs, IT and admins.
- **Access requests.** Nobody grants themselves access. A request (name, work email, level, lines, reason, PIN) waits until an admin approves it, possibly at a lower level, or rejects it. Both are audited. The request form never tells the requester whether a PIN is already in use (that would let someone discover other people's PINs); the admin sees a warning instead. Common PINs such as 1234 and 0000 are refused.
- **Wall screens** run in kiosk mode: board and OEE only, no menu, no inputs, no personal data beyond the name of whoever acknowledged a call.
- **Need to know on the shop floor.** A screen at a line shows only that line (line screen). The management overview with all lines is offered only to line managers, viewers, engineers and admins; typing its address without that access shows a line screen instead.
- **Device key** (`DEVICE_KEY`). Operator tablets and TV boards only open with the key in their link. Changing the key revokes every device at once.
- **Company sign-in (single sign-on, optional).** Add an `[auth]` section to the secrets, using Microsoft Entra ID, Okta, Google or any OpenID Connect provider. The responder, machines, analytics and admin pages then require a company account. `AUTH_ALLOWED_DOMAINS` limits sign-in to the company domain.
- **PINs** are hashed with PBKDF2 and a salt. After 5 wrong PINs from one device, PIN entry is locked for 10 minutes. Sessions last one shift (8 hours); admins and engineers are signed out after 30 minutes without activity.
- **Audit log.** Every call raised, line stop, acknowledgement, resolution, cancellation, configuration change, sign-in and wrong PIN is recorded with who, when and from which device or IP. You can view it and export it under Admin → Security. With the database roles below, the app can add events but cannot edit or delete them.
- **Security status page** (Admin → Security). It flags default PINs, demo mode, missing sign-in, a missing device key and an unencrypted database.
- **Safe display.** Names and notes are escaped before display, and all database access uses parameterised queries, which protects against SQL injection.

**Private demo (`PRIVATE_DEMO = "1"`)**
- Anonymous visitors can only open a welcome page and send an access request; every other page is not registered for them.
- Published demo PINs are disabled at start-up; the admin PIN comes from the secrets (8+ digits). New PINs need 6+ digits and are checked against obvious patterns.
- Rate limits on requests (3 per visitor, 60 in total per hour) and a global pause after 40 wrong PINs in 10 minutes, on top of the per-device lock-out.
- Requesters' names and emails are personal data: they are shown only to admins, used only to decide on access, and can be deleted on request (GDPR).

**Notifications**
- Sent by the app (Level 4), **never by the connector**, so the plant network keeps no route to the internet.
- Only HTTPS webhooks are accepted. Webhook URLs and phone numbers are masked on screen, and can be kept out of the database entirely with `${SECRET_NAME}`.
- Messages contain line, unit, problem, duration and role: no personal data beyond the name of whoever acknowledged a call. Check with IT before sending plant data to an external service such as Twilio.
- Once per call and event, at most 20 per channel per 10 minutes. Test messages and channel changes are audited.

**MES / ERP batches**
- The `mes` source is read-only: SQL queries must be a `SELECT`, REST must use HTTPS (plain HTTP only with an explicit setting inside an isolated network), tokens come from environment variables. Use a read-only MES reporting account or view.

**Database**
- `sql/roles.sql` creates two least-privilege accounts:
  - **`andon_app`** can work with calls and configuration, but can only append to the audit log.
  - **`andon_collector`** can read configuration, write signals, track events and batches, roll up OEE and raise automatic calls. It can add new events to the catalogue but cannot rename them, and it cannot read PIN hashes or change lines, rules, call types or targets. It has no access to notification channels.
  - The owner account is only needed to create tables at first start and after upgrades.

## Data integrity and regulated plants (GMP)

- The Andon and OEE boards are **informational tools for people on the floor**. They are not a batch record, an electronic signature system or a validated computerised system under EU GMP Annex 11 / 21 CFR Part 11.
- Batch numbers, products and destinations shown on the OEE board come from MES/ERP through the connector, or are typed by a supervisor. **MES and the electronic batch record remain the system of record**; never release or reject a batch based on this app.
- If a plant wants to rely on it for GMP decisions, it would need a validation project (requirements, risk assessment, IQ/OQ/PQ, change control), which is outside this demo.
- Event renames keep the original code and text, and every change is audited, so the meaning of what a machine reported is never lost.

## Before connecting a real plant: checklist

- [ ] **The OT/IT security team has approved the data flow**: which systems, which ports, in which direction.
- [ ] **The connector runs in the DMZ** or on an approved Level 3 server, with read-only accounts on SCADA, Kafka, the broker and the historian.
- [ ] **Encryption is on everywhere**: OPC UA Sign & Encrypt, MQTT/Kafka TLS, and HTTPS push with `allowed_ips`. There are no warnings in the connector log.
- [ ] **The app is hosted privately** (company network, private cloud or private Streamlit app), with company sign-in configured.
- [ ] **A `DEVICE_KEY` is set**, and tablets and boards use the links from Admin → Security.
- [ ] **Accounts are personal and not demo ones**: default and demo PINs are replaced by personal PINs, and demo mode is off.
- [ ] **An admin owns access requests** and reviews users and levels regularly (leavers disabled, levels still right).
- [ ] **The event catalogue is reviewed** by process engineering before go-live, so operators see clear names and OEE categories are right.
- [ ] **The database is managed**, with TLS, backups, and the two least-privilege accounts from `sql/roles.sql`.
- [ ] **Secrets are stored safely**, in the hosting platform's secret store or environment variables, never in files or Git.
- [ ] **Someone owns operations**: monitoring of the connector log and app availability, a patching routine and a named owner.
- [ ] **Data protection is agreed.** Responder names and the audit log are personal data. Agree a retention period, and inform employees and the works council (samarbejdsudvalg) before using response times per person. OEE is meant to improve lines, not to rate individual operators.

## What the app does not do, and who should

- **Network.** Firewalls, network segmentation, VPN and certificate management belong to plant IT/OT.
- **Testing.** Vulnerability scanning and penetration testing should be done before a production rollout.
- **Resilience.** High availability for the app and database: run two app instances behind a load balancer, with a managed database.
- **Remote control.** By design, the app never sends commands to machines. Andon should inform people, not control equipment.
