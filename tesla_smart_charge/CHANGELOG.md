# Changelog

## 0.1.1
- max_amps always wins over min_amps (startup warning; min is lowered to max). Hard 48 A ceiling in schema and engine.
- Stale guard: battery level or plug state older than `stale_minutes` (default 30, from last_reported/last_updated)
  counts as unknown: never starts or raises amps; may press wake to refresh; peak safety stop still fires.
- `dry_run_safety_off: true` listed in default options.

## 0.1.0
- First public version: lowest-amp off-peak charging to a target by departure, BLE first with cloud fallback,
  peak-off edge start, peak fail-safe, charge-now / pause / trip-mode helpers, not-charging notification.
  Installs in dry run.
