-- DDL for: public.tbl_comp_connections
-- Notes:
-- 1) Requires pgcrypto for gen_random_uuid()
-- 2) Creates table + FKs + indexes (including GIN on metadata)

BEGIN;

-- Needed for gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Create table
CREATE TABLE IF NOT EXISTS public.tbl_comp_connections (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,

  funnel_id uuid NOT NULL,
  from_component_id uuid NOT NULL,
  from_port_id varchar(255) NOT NULL,

  to_component_id uuid NOT NULL,
  to_port_id varchar(255) NOT NULL,

  metadata jsonb NULL,

  created_by bigint NULL,
  updated_by bigint NULL,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Foreign keys
-- Funnel FK (assumes table exists: public.tbl_funnels(id))
ALTER TABLE public.tbl_comp_connections
  DROP CONSTRAINT IF EXISTS fk_tbl_comp_connections_funnel_id;

ALTER TABLE public.tbl_comp_connections
  ADD CONSTRAINT fk_tbl_comp_connections_funnel_id
  FOREIGN KEY (funnel_id)
  REFERENCES public.tbl_funnels(id)
  ON UPDATE CASCADE
  ON DELETE CASCADE;

-- Users FK (assumes table exists: public.tbl_users(id))
ALTER TABLE public.tbl_comp_connections
  DROP CONSTRAINT IF EXISTS fk_tbl_comp_connections_created_by;

ALTER TABLE public.tbl_comp_connections
  ADD CONSTRAINT fk_tbl_comp_connections_created_by
  FOREIGN KEY (created_by)
  REFERENCES public.tbl_users(id)
  ON UPDATE CASCADE
  ON DELETE SET NULL;

ALTER TABLE public.tbl_comp_connections
  DROP CONSTRAINT IF EXISTS fk_tbl_comp_connections_updated_by;

ALTER TABLE public.tbl_comp_connections
  ADD CONSTRAINT fk_tbl_comp_connections_updated_by
  FOREIGN KEY (updated_by)
  REFERENCES public.tbl_users(id)
  ON UPDATE CASCADE
  ON DELETE SET NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_funnel_id
  ON public.tbl_comp_connections (funnel_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_from_component_id
  ON public.tbl_comp_connections (from_component_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_to_component_id
  ON public.tbl_comp_connections (to_component_id);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_created_at
  ON public.tbl_comp_connections (created_at);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_created_by
  ON public.tbl_comp_connections (created_by);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_updated_by
  ON public.tbl_comp_connections (updated_by);

CREATE INDEX IF NOT EXISTS idx_tbl_comp_connections_metadata_gin
  ON public.tbl_comp_connections
  USING gin (metadata);

COMMIT;
