-- 074: the leased-lane index for K claimers (07 §20; tap plan phase 3b).
-- Identical to the 07 §20 block; the advertised-DDL manifest pins the two together.
-- runner:postcondition SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ix_jobs_leased_lane_ws')

-- Phase 3b of the 2026-09-09 tap plan (K claim-and-run tasks per lane): `fn_claim_job` counts
-- a workspace's leased rows on the lane for the per-workspace cap on EVERY claim, and K claimers
-- per lane make that K times as many counts. The partial index serves exactly that predicate;
-- `uq_jobs_serialized_lease` (the one-key-one-runner proof) and the claim/expiry indexes stand.
CREATE INDEX ix_jobs_leased_lane_ws ON jobs (lane, workspace_id) WHERE state = 'leased';
