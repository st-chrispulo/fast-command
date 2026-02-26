ALTER TABLE tbl_socket_servers
ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

CREATE INDEX IF NOT EXISTS idx_tbl_socket_servers_last_seen_at
ON tbl_socket_servers (last_seen_at);
