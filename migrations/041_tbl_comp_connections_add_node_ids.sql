-- 041_tbl_comp_connections_add_node_ids.sql
-- Adds: from_node_id (text), to_node_id (text)

BEGIN;

ALTER TABLE public.tbl_comp_connections
  ADD COLUMN IF NOT EXISTS from_node_id text NULL;

ALTER TABLE public.tbl_comp_connections
  ADD COLUMN IF NOT EXISTS to_node_id text NULL;

-- Optional (recommended) indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_from_node_id
  ON public.tbl_comp_connections (from_node_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_to_node_id
  ON public.tbl_comp_connections (to_node_id);

COMMIT;
