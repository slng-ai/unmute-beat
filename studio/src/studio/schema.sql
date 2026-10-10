-- The studio's store. Postgres runs this once, on the first start of an empty volume
-- (compose.yaml mounts it into /docker-entrypoint-initdb.d). A change here needs
-- `uv run slng studio down --volumes` until migrations exist.
--
-- Runs are append-only: every event the engine publishes is kept in `events`, in order.
-- A turn's snapshot is what a fork starts from: the state and history after that turn.

CREATE TABLE agent_configs (
    agent_id     text        NOT NULL,
    version      text        NOT NULL,           -- sha256 of agent.yaml and every file it names, first 12 hex
    path         text        NOT NULL,           -- agent.yaml, relative to the repository root
    manifest     text        NOT NULL,           -- agent.yaml as saved
    files        jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- prompt and include files by relative path
    parent       text,                            -- the version this one was edited from
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (agent_id, version)
);

CREATE TABLE runs (
    id           text        PRIMARY KEY,         -- run_<hex>
    agent_id     text        NOT NULL,
    config       text        NOT NULL,           -- agent_configs.version it ran on
    channel      text        NOT NULL CHECK (channel IN ('voice', 'text', 'twilio')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    ended_at     timestamptz,
    outcome      text,                            -- run.ended outcome; NULL while live
    forked_from  text        REFERENCES runs (id),
    forked_turn  integer,
    FOREIGN KEY (agent_id, config) REFERENCES agent_configs (agent_id, version)
);
CREATE INDEX runs_by_agent ON runs (agent_id, started_at DESC);

CREATE TABLE events (
    run_id       text        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    seq          bigint      NOT NULL,           -- per run, from 0, no gaps
    turn         integer     NOT NULL,
    type         text        NOT NULL,           -- studio.events type, e.g. 'model.delta'
    t_ms         double precision NOT NULL,      -- milliseconds since run.started
    body         jsonb       NOT NULL,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX events_by_turn ON events (run_id, turn);

CREATE TABLE snapshots (
    run_id       text        NOT NULL REFERENCES runs (id) ON DELETE CASCADE,
    turn         integer     NOT NULL,
    step         text        NOT NULL,           -- where the caller was when the turn ended
    state        jsonb       NOT NULL,           -- the application state, whole (FlowState facts)
    history      jsonb       NOT NULL,           -- the call's messages, as Pydantic AI serialises them
    PRIMARY KEY (run_id, turn)
);
