# Changelog

## 0.1.4
- Charge-on retries use the same backoff (10/20/40/80/120 min) with a per-window cap (`charge_on_max_per_window`,
  default 5); reset only on a real plug-in or a new off-peak window. Repeats log at debug.
- Unknown/unavailable charging sensor is treated as unknown, not "not charging": peak, pause and safety stops are
  still sent.
- L-a: with `dry_run: false`, starts are refused (stops still work) unless `charge_limit` or `ble_charge_limit` is
  set, so a stale SoC restored after an HA restart can't charge without a ceiling.
- L-b: check cadence and wake backoff run on the monotonic clock; wall-clock jumps no longer delay re-plans.
- L-c: state timestamps more than 60 s in the future count as stale.
- L-e: a wake attempt with no wake button configured no longer uses up the wake budget.

## 0.1.3
- Wake backoff and per-window cap reset only on a real plug off -> on, or a new off-peak window. Fresh data no
  longer resets them, and unavailable/unknown readings never count as a transition (SA L4: plug flapping).
- Hard 10-minute minimum between any two wake presses (monotonic clock, never reset), covering both the stale
  path and the normal start path.
- Peak window edges come only from definite readings: a peak sensor flapping unavailable <-> off no longer opens a
  new window (which would also have reset the notify-once flag and cleared pause).
- Self-review: the normal start path's wake (car asleep, fresh data) now shares the same per-window cap and
  backoff as the stale path, so a car that never wakes is not pressed every check forever.

## 0.1.2
- Stale-state wake (SA L3): wake is pressed only when the plug is freshly known plugged in, the optional
  `home_entity` says home, it is off-peak (or charge-now is on) and the last known SoC is below target.
  Exponential backoff 10, 20, 40, 80, then 120 min (cap); at most `wake_max_per_window` (default 5) wakes per
  window; resets on fresh data, plug change or a new off-peak window.

## 0.1.1
- max_amps always wins over min_amps (startup warning; min is lowered to max). Hard 48 A ceiling in schema and engine.
- Stale guard: battery level or plug state older than `stale_minutes` (default 30, from last_reported/last_updated)
  counts as unknown: never starts or raises amps; may press wake to refresh; peak safety stop still fires.
- `dry_run_safety_off: true` listed in default options.

## 0.1.0
- First public version: lowest-amp off-peak charging to a target by departure, BLE first with cloud fallback,
  peak-off edge start, peak fail-safe, charge-now / pause / trip-mode helpers, not-charging notification.
  Installs in dry run.
