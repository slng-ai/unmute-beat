-- The studio's store, applied by the studio on every start (`studio.store`): every statement may run again.
-- compose.yaml also mounts it into /docker-entrypoint-initdb.d, so an empty volume starts with it.
--
-- Runs are append-only: every event a runner sends is kept in `events`, in order. A turn's snapshot
-- (`core.snapshot.TurnSnapshot`) is what going back starts from. Redis holds the same for a week and
-- serves the live view; Postgres keeps them.

CREATE TABLE IF NOT EXISTS agent_configs (
    agent_id     text        NOT NULL,
    version      text        NOT NULL,           -- sha256 of agent.yaml and every file in its folder, first 12 hex
    path         text        NOT NULL,           -- agent.yaml, relative to the repository root
    manifest     text        NOT NULL,           -- agent.yaml as saved
    files        jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- prompt files by relative path
    parent       text,                            -- the version this one was edited from
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, version)
);

CREATE TABLE IF NOT EXISTS runs (
    id           text        PRIMARY KEY,         -- run_<hex>
    agent_id     text        NOT NULL,
    config       text        NOT NULL,           -- agent_configs.version it ran on
    channel      text        NOT NULL CHECK (channel IN ('voice', 'text', 'twilio')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    outcome      text,                            -- run.ended outcome; NULL while live
    forked_from  text,
    forked_turn  integer,
    runner       text,
    turns        integer     NOT NULL DEFAULT 0
);
ALTER TABLE runs ADD COLUMN IF NOT EXISTS runner text;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS turns integer NOT NULL DEFAULT 0;
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_agent_id_config_fkey;
ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_forked_from_fkey;
CREATE INDEX IF NOT EXISTS runs_by_agent ON runs (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS runs_by_start ON runs (started_at DESC);

CREATE TABLE IF NOT EXISTS events (
    run_id       text        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq          bigint      NOT NULL,           -- per run, from 0
    turn         integer     NOT NULL,
    type         text        NOT NULL,           -- core.events type, e.g. 'model.delta'
    t_ms         double precision NOT NULL,      -- milliseconds since run.started
    body         jsonb       NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS events_by_turn ON events (run_id, turn);

CREATE TABLE IF NOT EXISTS snapshots (
    run_id       text        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    turn         integer     NOT NULL,
    step         text        NOT NULL,           -- where the caller was when the turn ended
    state        jsonb       NOT NULL,           -- the application's facts, whole
    history      jsonb       NOT NULL,           -- the call's messages, as Pydantic AI serialises them
    PRIMARY KEY (run_id, turn)
);
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS snapshot jsonb;  -- the whole TurnSnapshot
