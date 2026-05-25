-- Migration: 037_fix_dry_run_mode_default.sql
-- Description: Fix dry_run_mode column default from TRUE to FALSE
-- Created: 2026-05-25
--
-- Migration 006 created chat_settings with `dry_run_mode BOOLEAN DEFAULT true`,
-- contradicting the code-level DEFAULT_DRY_RUN_MODE = False. Commit 6ca43d3 fixed
-- the SQLAlchemy model default but not the PostgreSQL column default. Any row
-- created via raw SQL, migration, or ORM without an explicit value still gets
-- dry_run_mode = true, silently blocking Instagram posting.
--
-- This migration:
-- 1. Changes the column default to FALSE (matches code intent)
-- 2. Flips existing rows that were stuck on the wrong default

-- Fix the column default so future rows get FALSE
ALTER TABLE chat_settings ALTER COLUMN dry_run_mode SET DEFAULT false;

-- Fix existing rows stuck on the wrong default.
-- The code-level default has always been False; rows with True got it from the
-- incorrect DDL default rather than user intent (the toggle sets it explicitly
-- and the only toggle path is /settings which writes to the DB directly).
UPDATE chat_settings SET dry_run_mode = false WHERE dry_run_mode = true;

-- Record migration
INSERT INTO schema_version (version, description, applied_at)
VALUES (37, 'Fix dry_run_mode column default from TRUE to FALSE', NOW())
ON CONFLICT DO NOTHING;
