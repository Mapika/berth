-- Service profiles can target a specific cluster node, so the deploy-
-- from-profile flow ends up on the right host. NULL / 'local' = leader.
ALTER TABLE service_profiles ADD COLUMN node_label TEXT;
