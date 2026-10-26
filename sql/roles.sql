-- Least-privilege database accounts for Andon (PostgreSQL).
-- Run once as the database owner AFTER the app has started once (so the andon_* tables exist):
--   psql "$OWNER_DATABASE_URL" -v app_pw="'...'" -v collector_pw="'...'" -f sql/roles.sql
-- Then give the app  postgresql://andon_app:...@host/db        (Streamlit secrets: DATABASE_URL)
-- and the connector  postgresql://andon_collector:...@host/db  (collector config: database_url)

-- 1. Accounts ---------------------------------------------------------------
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'andon_app') THEN CREATE ROLE andon_app LOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'andon_collector') THEN CREATE ROLE andon_collector LOGIN; END IF;
END $$;
ALTER ROLE andon_app PASSWORD :app_pw;
ALTER ROLE andon_collector PASSWORD :collector_pw;

-- Nobody else gets anything by default
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO andon_app, andon_collector;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO andon_app, andon_collector;

-- 2. The app (screens): reads everything, changes calls and configuration ---
GRANT SELECT, INSERT, UPDATE, DELETE ON
  andon_lines, andon_stations, andon_call_types, andon_reasons, andon_responders, andon_calls,
  andon_settings, andon_signal_defs, andon_rules, andon_rule_state, andon_signal_latest, andon_signal_history,
  andon_events, andon_event_log, andon_boards, andon_batches, andon_oee_hourly, andon_oee_targets, andon_handover,
  andon_notify_channels
  TO andon_app;
-- The notification log is append-and-update only (status of a message being sent), never deleted by the app
GRANT SELECT, INSERT, UPDATE ON andon_notify_log TO andon_app;
-- The audit log is append-only for the app: it can add and read events, never edit or delete them
GRANT SELECT, INSERT ON andon_audit TO andon_app;

-- 3. The connector: reads configuration, writes signals, raises auto-calls ---
GRANT SELECT ON andon_lines, andon_stations, andon_call_types, andon_signal_defs, andon_rules, andon_settings,
  andon_oee_targets TO andon_collector;
GRANT SELECT, INSERT, UPDATE ON andon_signal_latest TO andon_collector;
GRANT SELECT, INSERT, DELETE ON andon_signal_history TO andon_collector;      -- DELETE only to prune old history
GRANT SELECT, INSERT, UPDATE, DELETE ON andon_rule_state TO andon_collector;
GRANT SELECT, INSERT, UPDATE ON andon_calls TO andon_collector;               -- raise auto-calls, mark line stopped
GRANT INSERT ON andon_settings TO andon_collector;
GRANT UPDATE (value) ON andon_settings TO andon_collector;            -- simulator switch, OEE roll-up timestamp
GRANT SELECT, INSERT ON andon_events TO andon_collector;                      -- new alarm texts get a catalogue entry
                                                                              -- (it cannot rename them: no UPDATE)
GRANT SELECT, INSERT, UPDATE ON andon_event_log TO andon_collector;           -- start and end of each event
GRANT SELECT, INSERT, UPDATE ON andon_batches TO andon_collector;             -- batch_no / product / destination keys
GRANT SELECT, INSERT, DELETE ON andon_oee_hourly TO andon_collector;          -- hourly OEE roll-up
GRANT INSERT ON andon_audit TO andon_collector;
-- The connector cannot read responders (PIN hashes) and cannot change lines, call types or rules.
-- It has no access to notification channels: notifications are sent by the app, so the plant network needs no internet.
