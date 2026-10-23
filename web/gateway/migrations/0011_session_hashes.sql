-- Data rewrite implemented transactionally in db._apply_python_migration.
-- PostgreSQL core has no SHA-256 function and supported deployments do not
-- require pgcrypto, so keeping the version marker as a SQL no-op avoids an
-- extension dependency while preserving the ordered migration ledger.
SELECT 1;
