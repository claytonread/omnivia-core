-- Stopping a run, and stopping admission for a whole workspace (RT-207).
--
-- Additive only. Two append-only tables, their guards, and two further BEFORE INSERT
-- triggers on relations 0018 and 0023 already created. Nothing here creates a run,
-- terminalizes one, resolves a wait, finishes an attempt, settles an effect or deletes
-- a single row of anything.
--
--   omnivia_runtime_run_stops         the one stop command a run is given
--   omnivia_runtime_admission_stops   the workspace's emergency admission-stop ledger
--
-- *A stop is a command, not an outcome.* This migration stores the *request* -- why the
-- run is being stopped and what is to happen to work already running -- and stores no
-- status, no terminal instant and no result. What the run finally becomes is derived
-- from this row plus the run's own open work by
-- :func:`~service.runtime_stop.decide_stop_settlement` and recorded where a run's status
-- has always lived: as one more entry on the append-only event stream 0018 owns, under
-- 0018's own transition and terminal-sink rules. There is deliberately no second place a
-- run's status could be written down, because a status with two homes is a status that
-- can disagree with itself.
--
-- *One stop per run, keyed on the run.* `UNIQUE (workspace_id, run_id)` is what makes a
-- repeated stop command a replay rather than a second decision: a caller re-issuing its
-- own command after a crash finds the identical row and writes nothing, and a caller
-- asking for a *different* stop of the same run is asking this schema to change an
-- answer rather than repeat it, and has nowhere to write it. UPDATE and DELETE abort
-- unconditionally, for the current fenced owner too.
--
-- *A finished run is never stopped.* The insert refuses a run whose stream already holds
-- a terminal event. Cancelling a run that succeeded would be re-deciding a concluded
-- outcome, and cancelling one that already failed would add a second cause of death; in
-- both cases the honest answer is that there is nothing left to stop. `uncertain` is not
-- terminal and is not refused: an uncertain run is precisely one that may still need
-- stopping.
--
-- *`superseded` names its successor and the other two name nobody.* A supersession that
-- could not say what superseded the run would be indistinguishable from a cancellation,
-- and a cancellation carrying a successor would imply a handover nobody performed. The
-- successor is a run of this same workspace and is never the run being stopped.
--
-- *The running-work policy is explicit and is never inferred.* `await` means the attempts
-- and waits already open settle on their own and the run terminalizes only once they
-- have; `release` means this stop closes them -- pending waits `cancelled`, running
-- attempts `cancelled`, unfinished steps `cancelled` -- through the same append-only
-- relations any other closure uses. There is no third value and no default, because
-- "what happens to work that is already running" is exactly the question an operator
-- must answer rather than a question this schema may answer for them.
--
-- *The emergency admission stop is a ledger, not a flag.* One append-only sequence per
-- workspace, opening at `engaged` and alternating, so engaging and releasing are both
-- durable facts with instants rather than a boolean somebody flipped. The entry with the
-- highest sequence is the one in force. An `engaged` entry states the running-work
-- policy the whole workspace is stopping under; a `released` entry states none, because
-- releasing the stop declares nothing about work.
--
-- *What an engaged stop actually stops.* Two triggers, on `omnivia_runtime_runs` and on
-- `omnivia_runtime_effect_intents`: while a stop is engaged no run is admitted and no
-- effect is intended, in this database, by any writer, whatever code path it came from.
-- Structural rather than policed, for the reason 0023 makes "no effect before intent"
-- structural. Nothing else is blocked -- a dispatch of an intent already declared, a
-- receipt, a settlement, a reconciliation, a wait resolution, an attempt outcome and a
-- run event all still land -- because work already running has to be able to *finish*.
-- An emergency stop that also froze the work in flight would leave every uncertain
-- effect uncertain forever, which is the opposite of stopping safely.
--
-- *A run stop issued under an engaged emergency stop obeys it.* The stop's running-work
-- policy must equal the engaged entry's. That is what makes the ledger's policy a rule
-- rather than a note: an operator who declared that running work settles cannot then be
-- overruled one run at a time.
--
-- What is deliberately absent. No status, outcome or terminal-instant column, for the
-- reason above. No retry, attempt or redispatch column: stopping never produces work, and
-- an effect settled `unknown` is never retried by anything here. No deletion or rewriting
-- of any prior event, evidence, artifact, receipt or settlement -- a stopped run keeps
-- every fact it accumulated, and that is the whole point of terminalizing through the
-- event stream instead of collapsing the record. And no DML, because no fact already in
-- this database is honestly classifiable as a stop anybody commanded.
--
-- Identity, types, bounds and timestamps follow 0018's, 0023's and 0024's rules exactly.
-- Every comment in this file sits between statements and never inside one, for the
-- fingerprint-loader reason 0018 states.

CREATE TABLE IF NOT EXISTS omnivia_runtime_run_stops (
    workspace_id         TEXT    NOT NULL,
    run_stop_id          TEXT    NOT NULL,
    run_id               TEXT    NOT NULL,
    stop_reason          TEXT    NOT NULL,
    running_work         TEXT    NOT NULL,
    requested_at_us      INTEGER NOT NULL,
    audit_ref            TEXT    NOT NULL,
    superseded_by_run_id TEXT,

    PRIMARY KEY (workspace_id, run_stop_id),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(run_stop_id) = 'text' AND length(run_stop_id) BETWEEN 1 AND 128
           AND run_stop_id GLOB '[A-Za-z0-9]*'
           AND run_stop_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_stop_id, char(0)) = 0),
    CHECK (typeof(run_id) = 'text' AND length(run_id) BETWEEN 1 AND 128
           AND run_id GLOB '[A-Za-z0-9]*'
           AND run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(run_id, char(0)) = 0),
    CHECK (stop_reason IN ('cancelled', 'timed_out', 'superseded')),
    CHECK (running_work IN ('await', 'release')),
    CHECK (typeof(requested_at_us) = 'integer' AND requested_at_us > 0),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),
    CHECK (superseded_by_run_id IS NULL OR (typeof(superseded_by_run_id) = 'text'
           AND length(superseded_by_run_id) BETWEEN 1 AND 128
           AND superseded_by_run_id GLOB '[A-Za-z0-9]*'
           AND superseded_by_run_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(superseded_by_run_id, char(0)) = 0)),
    CHECK ((stop_reason = 'superseded' AND superseded_by_run_id IS NOT NULL)
           OR (stop_reason <> 'superseded' AND superseded_by_run_id IS NULL)),
    CHECK (superseded_by_run_id IS NULL OR superseded_by_run_id <> run_id),

    UNIQUE (workspace_id, run_id),

    FOREIGN KEY (workspace_id, run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (workspace_id, superseded_by_run_id)
        REFERENCES omnivia_runtime_runs (workspace_id, run_id),
    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS omnivia_runtime_admission_stops (
    workspace_id      TEXT    NOT NULL,
    sequence          INTEGER NOT NULL,
    admission_stop_id TEXT    NOT NULL,
    state             TEXT    NOT NULL,
    running_work      TEXT,
    effective_at_us   INTEGER NOT NULL,
    reason            TEXT    NOT NULL,
    audit_ref         TEXT    NOT NULL,

    PRIMARY KEY (workspace_id, sequence),

    CHECK (typeof(workspace_id) = 'text' AND length(workspace_id) BETWEEN 1 AND 128
           AND workspace_id GLOB '[A-Za-z0-9]*'
           AND workspace_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(workspace_id, char(0)) = 0),
    CHECK (typeof(sequence) = 'integer' AND sequence BETWEEN 0 AND 999),
    CHECK (typeof(admission_stop_id) = 'text'
           AND length(admission_stop_id) BETWEEN 1 AND 128
           AND admission_stop_id GLOB '[A-Za-z0-9]*'
           AND admission_stop_id NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(admission_stop_id, char(0)) = 0),
    CHECK (state IN ('engaged', 'released')),
    CHECK (running_work IS NULL OR running_work IN ('await', 'release')),
    CHECK ((state = 'engaged' AND running_work IS NOT NULL)
           OR (state = 'released' AND running_work IS NULL)),
    CHECK (typeof(effective_at_us) = 'integer' AND effective_at_us > 0),
    CHECK (typeof(reason) = 'text' AND length(reason) BETWEEN 1 AND 128
           AND reason GLOB '[a-z]*' AND reason NOT GLOB '*[^a-z0-9_.]*'
           AND reason NOT GLOB '*.' AND reason NOT GLOB '*.[^a-z]*'),
    CHECK (typeof(audit_ref) = 'text' AND length(audit_ref) BETWEEN 1 AND 128
           AND audit_ref GLOB '[A-Za-z0-9]*'
           AND audit_ref NOT GLOB '*[^A-Za-z0-9._:-]*'
           AND instr(audit_ref, char(0)) = 0),

    UNIQUE (workspace_id, admission_stop_id),

    FOREIGN KEY (audit_ref, workspace_id)
        REFERENCES omnivia_application_audit_events (audit_ref, workspace_id)
) WITHOUT ROWID;

-- The two INSERT guards below repeat the complete connection-authority, mutation-guard,
-- workspace-state and lease predicate 0005 established, then add the correlation and
-- ordering rules a stop has. UPDATE and DELETE abort unconditionally on both tables --
-- for the current fenced owner too, because a stop command the owner may rewrite is not
-- a command, and a stopped run whose history could be edited would lose the evidence the
-- stop exists to preserve.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_stops_insert
BEFORE INSERT ON omnivia_runtime_run_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_run_stops')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: a run is never stopped before it was admitted')
    WHERE NEW.requested_at_us < (
        SELECT created_at_us FROM omnivia_runtime_runs
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id);
    SELECT RAISE(ABORT, 'omnivia: a run that has already finished cannot be stopped')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_events
        WHERE workspace_id = NEW.workspace_id AND run_id = NEW.run_id
          AND run_status IN ('succeeded', 'partially_completed', 'failed', 'cancelled'));
    SELECT RAISE(ABORT, 'omnivia: a run stop must obey the engaged admission stop running-work policy')
    WHERE EXISTS (
        SELECT 1 FROM omnivia_runtime_admission_stops a
        WHERE a.workspace_id = NEW.workspace_id
          AND a.sequence = (
            SELECT MAX(sequence) FROM omnivia_runtime_admission_stops
            WHERE workspace_id = NEW.workspace_id)
          AND a.state = 'engaged'
          AND a.running_work <> NEW.running_work);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_stops_update
BEFORE UPDATE ON omnivia_runtime_run_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_stops is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_run_stops_delete
BEFORE DELETE ON omnivia_runtime_run_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_run_stops is append-only; DELETE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_stops_insert
BEFORE INSERT ON omnivia_runtime_admission_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: unguarded INSERT on omnivia_runtime_admission_stops')
    WHERE omnivia_service_writer() IS NOT 1
       OR NOT EXISTS (
            SELECT 1 FROM omnivia_mutation_guard g
            JOIN omnivia_workspace_state s ON s.singleton = 1
            JOIN omnivia_workspace_lease l ON l.singleton = 1
            WHERE g.singleton = 1 AND g.fencing_generation = s.fencing_generation
              AND g.workspace_id = s.workspace_id
              AND l.fencing_generation = g.fencing_generation
              AND l.workspace_id = g.workspace_id
              AND l.service_instance_id = g.service_instance_id
              AND l.lifecycle IN ('acquiring', 'held', 'draining'))
       OR NEW.workspace_id IS NOT (
            SELECT workspace_id FROM omnivia_workspace_state WHERE singleton = 1);
    SELECT RAISE(ABORT, 'omnivia: admission stop sequence must be contiguous from zero')
    WHERE NEW.sequence IS NOT (
        SELECT COALESCE(MAX(sequence), -1) + 1 FROM omnivia_runtime_admission_stops
        WHERE workspace_id = NEW.workspace_id);
    SELECT RAISE(ABORT, 'omnivia: an admission stop ledger opens by engaging a stop')
    WHERE NEW.sequence = 0 AND NEW.state <> 'engaged';
    SELECT RAISE(ABORT, 'omnivia: an admission stop entry that restates its predecessor records nothing')
    WHERE NEW.sequence > 0
      AND NEW.state IS (
        SELECT state FROM omnivia_runtime_admission_stops
        WHERE workspace_id = NEW.workspace_id AND sequence = NEW.sequence - 1);
    SELECT RAISE(ABORT, 'omnivia: admission stop time must not regress')
    WHERE NEW.sequence > 0
      AND NEW.effective_at_us < (
        SELECT effective_at_us FROM omnivia_runtime_admission_stops
        WHERE workspace_id = NEW.workspace_id AND sequence = NEW.sequence - 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_stops_update
BEFORE UPDATE ON omnivia_runtime_admission_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_admission_stops is append-only; UPDATE is never permitted');
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_admission_stops_delete
BEFORE DELETE ON omnivia_runtime_admission_stops
BEGIN
    SELECT RAISE(ABORT, 'omnivia: omnivia_runtime_admission_stops is append-only; DELETE is never permitted');
END;

-- What an engaged emergency stop denies, stated on the two relations that admit new
-- work rather than in the code paths that reach them. A second BEFORE INSERT trigger on
-- a table 0018 and 0023 already guard: SQLite runs both, either may abort, and neither
-- weakens the other. Admitting a run and declaring an effect intent are the only two
-- writes refused; every relation that lets already-running work reach an outcome is
-- deliberately left open.

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_runs_admission_stop_insert
BEFORE INSERT ON omnivia_runtime_runs
BEGIN
    SELECT RAISE(ABORT, 'omnivia: an emergency admission stop is engaged; no run is admitted')
    WHERE 'engaged' IS (
        SELECT state FROM omnivia_runtime_admission_stops
        WHERE workspace_id = NEW.workspace_id ORDER BY sequence DESC LIMIT 1);
END;

CREATE TRIGGER IF NOT EXISTS omnivia_guard_runtime_effect_intents_admission_stop_insert
BEFORE INSERT ON omnivia_runtime_effect_intents
BEGIN
    SELECT RAISE(ABORT, 'omnivia: an emergency admission stop is engaged; no effect is intended')
    WHERE 'engaged' IS (
        SELECT state FROM omnivia_runtime_admission_stops
        WHERE workspace_id = NEW.workspace_id ORDER BY sequence DESC LIMIT 1);
END;
