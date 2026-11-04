# Changelog

## 0.3.0
- Fix: at/after the finish-by time the add-on stops adjusting instead of rolling over to tomorrow's departure
  (it used to command `min_amps` right at the deadline). Late plug-ins with no session still charge at max.
- Wall Connector safety: optional `wall_connector_status` / `handle_temp_sensor` (`handle_temp_max_f`, default
  150 F) force a safety STOP on a fault or hot handle (any plan, also in dry run), one notification per episode.
- Requested amps are clamped to the charge-current entity's `max` attribute (no more out-of-range requests).
- Derate detection via optional `actual_current_sensor`: never raises amps while the charger is limiting.
- Upward ramps limited to `ramp_up_max_a` (default 4 A) per plan; downward changes are immediate.
- Helper ids validated against `^input_boolean\.[a-z0-9_]+$` (schema and engine).
- Card restyled after the Tesla in-car charging screen (dark, battery bar with limit marker); device-specific
  sizing removed; fault / derate / deadline alerts.
- Tests: replay of a real overnight incident (hot handle, 6 A derate, fault, deadline rollover).

## 0.2.0
- Lovelace card `custom:tesla-smart-charge-card` (lovelace/): SoC ring vs target, plan amps, start / done-by /
  deadline, kWh needed, estimated cost, transport badge (Bluetooth / Cloud), alerts, and Smart / Charge now / Pause /
  Trip chips that toggle your input_boolean helpers. Reads only the status sensor.
- Taper-aware planning: above `taper_soc` (default 90) each kWh is planned at `taper_factor` (1.5x) the time, so
  100% targets finish on time instead of running late.
- TOU `rates` + `currency` for a session cost estimate.
- Rich status sensor attributes: mode, plan_amps, kWh needed, start_at, eta_finish, finish_by, est_cost, transport,
  setup_mode, next_action, last_error, helper entity ids.
- Startup self-check: logs cloud-only vs BLE+cloud, which configured entities resolved or are missing, and gaps
  (no peak source, required options empty).

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
