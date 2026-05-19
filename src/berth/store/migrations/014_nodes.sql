-- Multi-node support: nodes table, per-node GPU inventory, and a
-- deployments.node_id pointer. Single-node installs become the 'local'
-- node automatically (a row inserted by the daemon on first startup;
-- existing deployments default to node_id=0 here and are reassigned
-- to the local node's id on bootstrap).

CREATE TABLE nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    label           TEXT NOT NULL UNIQUE,
    fingerprint     TEXT NOT NULL,
    reachable_as    TEXT,
    status          TEXT NOT NULL DEFAULT 'unreachable',
    first_seen      REAL NOT NULL,
    last_seen       REAL NOT NULL,
    agent_version   TEXT,
    cpu_count       INTEGER NOT NULL DEFAULT 0,
    total_ram_mb    INTEGER NOT NULL DEFAULT 0,
    gpu_count       INTEGER NOT NULL DEFAULT 0,
    total_vram_mb   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE node_gpus (
    node_id         INTEGER NOT NULL,
    gpu_index       INTEGER NOT NULL,
    name            TEXT NOT NULL,
    total_vram_mb   INTEGER NOT NULL,
    driver_version  TEXT,
    PRIMARY KEY (node_id, gpu_index),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);

ALTER TABLE deployments ADD COLUMN node_id INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_deployments_node_id ON deployments(node_id);
