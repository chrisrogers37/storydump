-- Functions that table CHECK constraints call, created before the tables.

CREATE FUNCTION fn_safe_tz(p_tz text) RETURNS text LANGUAGE plpgsql STABLE AS $$
BEGIN
  PERFORM now() AT TIME ZONE p_tz;
  RETURN p_tz;
EXCEPTION WHEN invalid_parameter_value THEN
  RETURN 'UTC';
END $$;
