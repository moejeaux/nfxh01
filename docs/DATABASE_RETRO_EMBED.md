# Supabase / Postgres for embedded retrospective

The main process can run shallow and deep retrospectives when `retro.enabled` is true and `fathom_retrospective.embed_in_main_process` is true. Those paths use `DATABASE_URL` (same as the decision journal).

## Symptoms

- Log prefix `RETRO_EMBED_JOURNAL_QUERY_FAILED` with `host=...` and an error mentioning **authentication** or **connection** usually means the URL or network path is wrong, not the SQL text.

## Checklist (Mac Mini or any runner)

1. **`DATABASE_URL` is set** in the process environment (shell, systemd, or launchd). Missing URL is logged as `RETRO_EMBED_SKIP reason=DATABASE_URL_missing`.

2. **Match a working `psql` session**  
   Copy the exact URL you use for `psql "$DATABASE_URL" -c 'select 1'`. If that fails, fix credentials, host, port, or database name before tuning application code.

3. **Supabase pooler vs session**  
   Use the connection string from the Supabase dashboard for your workload (often the **pooler** for many short-lived clients). Wrong mode can cause auth or capacity errors.

4. **`sslmode=require`**  
   Append `?sslmode=require` (or include it in the URL query string) when the provider requires TLS. Omission can surface as opaque connection or SSL errors.

5. **Password rotation**  
   After Supabase password resets, update every deployment copy of `DATABASE_URL`.

6. **Network allowlists**  
   If the project restricts IPs, ensure the runner’s public IP is allowed.

## Log field `host=`

On `get_recent_retrospectives` failure, logs include **hostname and port only** (no user or password) so operators can confirm which endpoint the app hit.

## Retro advisor tuning

For deep runs where the model often omits JSON keys, tune `retro.temperature` and `retro.num_predict` in `config.yaml`. Repair attempts after a failed schema validation use `retro.advisor_retry_temperature` (defaults to `0.0` for deterministic retries).
