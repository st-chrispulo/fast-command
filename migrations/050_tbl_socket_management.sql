BEGIN;

CREATE TABLE IF NOT EXISTS tbl_socket_servers (
    id BIGSERIAL PRIMARY KEY,
    server_key TEXT UNIQUE NOT NULL,
    name TEXT NULL,
    host TEXT NULL,
    region TEXT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,

    max_rooms INTEGER NOT NULL DEFAULT 0,
    rooms_used INTEGER NOT NULL DEFAULT 0,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT chk_tbl_socket_servers_max_rooms_nonneg CHECK (max_rooms >= 0),
    CONSTRAINT chk_tbl_socket_servers_rooms_used_nonneg CHECK (rooms_used >= 0),
    CONSTRAINT chk_tbl_socket_servers_rooms_used_le_max CHECK (
        max_rooms = 0 OR rooms_used <= max_rooms
    )
);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_servers_is_active
ON tbl_socket_servers (is_active);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_servers_capacity
ON tbl_socket_servers (max_rooms, rooms_used);

CREATE TABLE IF NOT EXISTS tbl_socket_rooms (
    id BIGSERIAL PRIMARY KEY,
    server_id BIGINT NOT NULL REFERENCES tbl_socket_servers(id) ON DELETE CASCADE,
    room_key TEXT NOT NULL,
    room_type TEXT NOT NULL DEFAULT 'custom',
    join_token_hash TEXT NOT NULL,
    scope_json JSONB NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (server_id, room_key)
);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_rooms_server_id
ON tbl_socket_rooms (server_id);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_rooms_room_type
ON tbl_socket_rooms (room_type);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_rooms_is_active
ON tbl_socket_rooms (is_active);

CREATE INDEX IF NOT EXISTS gin_tbl_socket_rooms_scope_json
ON tbl_socket_rooms USING GIN (scope_json);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tbl_socket_rooms_join_token_hash
ON tbl_socket_rooms (join_token_hash);


CREATE TABLE IF NOT EXISTS tbl_socket_room_members (
    id BIGSERIAL PRIMARY KEY,
    room_id BIGINT NOT NULL REFERENCES tbl_socket_rooms(id) ON DELETE CASCADE,
    member_type TEXT NOT NULL DEFAULT 'service',
    member_key TEXT NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    left_at TIMESTAMPTZ NULL,
    meta_json JSONB NULL
);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_room_members_room_id
ON tbl_socket_room_members (room_id);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_room_members_member_key
ON tbl_socket_room_members (member_key);

CREATE INDEX IF NOT EXISTS idx_tbl_socket_room_members_left_at
ON tbl_socket_room_members (left_at);

CREATE UNIQUE INDEX IF NOT EXISTS uq_tbl_socket_room_members_active
ON tbl_socket_room_members (room_id, member_type, member_key)
WHERE left_at IS NULL;

CREATE INDEX IF NOT EXISTS gin_tbl_socket_room_members_meta_json
ON tbl_socket_room_members USING GIN (meta_json);

COMMIT;
