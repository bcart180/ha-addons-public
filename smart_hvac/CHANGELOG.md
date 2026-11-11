## 0.9.5 — 2026-11-11

- Mini splits can pause together when a room cools too fast while heating, using the room sensor's own report times over the last 20 minutes. The default trigger is a 5.4°F/hr fall held for 120 seconds. They resume when that slope is flat or rising, or after 30 minutes, whichever comes first. `window_slope` defaults to on. Set it to false for 0.9.4 behavior.
- The estimate is a straight lookback, not a short moving average. Room sensors that report only on a 1°F step would look like a steep drop to a short average and pause on ordinary reports. A single 1°F step over 20 minutes is about 3°F/hr and does not trip 5.4. The unit's own temperature is never the signal, including when the room sensor is stale.
- A pause waits out a setpoint drop or a mode change (45 minutes), defrost and the grace after it, outdoor air that is not at least 5°F colder than the room, and a third pause in the same hour. It runs only in heat. Both splits pause and resume on the same cycle. One a person already turned off stays off.
- The pause is the app's own off. It does not start a hold. A hold already running keeps its timer and its remembered temperature and pauses with the other split. After a restart the pause is still the app's off, so the splits can come back. If a pause is active, let it resume (or turn both splits back on) before rolling back to 0.9.4. 0.9.4 treats every off as a person's off after a restart and will not turn them back on. Remove the per-zone `window_slope_*` keys before that rollback; 0.9.4's schema does not know them. 0.9.4 ignores the new state-file keys.
- The radiant / bath floor zone is not part of this. Contact-sensor windows are unchanged. Heater safety, the spare-bath door pause, and the radiant over-temp shutoff are unchanged.
- Per zone, optional: `window_slope_threshold_f_per_h` (2–15, default 5.4), `window_slope_delay_s` (0–1800, default 120), `window_slope_max_min` (5–120, default 30), `window_slope_lookback_min` (10–60, default 20). While paused, Climate, the thermostat page, the classic page, and the Lovelace card say "Paused, window open?" `/api/state` adds a `window_slope` block on each zone.

## 0.9.4 — 2026-11-11

- Climate keeps its scroll place in Safari after a tap or refresh.

## 0.9.3 — 2026-11-11

- Each room remembers one override temperature, stored in the add-on state file as `override_temps`. It is separate from the Full Auto schedule. Saving it does not start a hold and does not change the unit. A hold uses that temperature, then keeps the number after the hold ends and after a restart. Until a number is saved, the hold stepper still drafts the way 0.9.2 did.
- While that hold is on, a mini split chooses heat or cool with the existing Full Auto rules (deadband, cool lockout, short cycle, and the shared outdoor unit). The override temperature is the target. Sun and humidity offsets are not applied to it. The radiant floor stays heat only. A person’s off, an open window, vacation, defrost, and a missing sensor still hold as before.
- `POST /api/zone/<name>/override-temp` with `{"temperature": <number>}` saves that number. `GET /api/state` shows it per room (`source: "zone"`) or `temperature: null` and `source: "unset"`.
- The Climate screen no longer offers Hold both in Heat or Hold both in Cool. Off · both remains. Heat and cool are chosen from the temperature. The header links Earlier layout and Thermostat cards are gone. `/classic` and `/thermostat` still open.
- Known leftover: the unlinked thermostat page and the Lovelace card still have a Heat / Cool control. This version does not change those.
- The options schema is unchanged. A 0.9.2 rollback leaves `override_temps` unused in the state file. Schedule preferences are kept.

## 0.9.2 — 2026-11-10

- Climate, classic, and thermostat screens use the same dark presentation as the Home trial: black page, `#414141` cards, `#757575` selected controls, `#005aff` for the active mark and the target line. Green `#3ddc5a` is only the current indoor temperature. Heating, cooling, holds, and defrost stay labeled in words. No amber, gold, or left stripe.
- Each room on the Climate screen shows its history graph with the temperature, before the schedule and hold controls. The shared history card, its range and room buttons, and every existing control stay. Opening the screen still only reads state.
- Controls, endpoints, ingress checks, mode lock, and the spare heater are unchanged.

## 0.9.1 — 2026-11-07

- Climate screen colors only. Layout, controls, and behavior are unchanged. The base stays black and gray.
- Heating is amber (`#e8a05a`) on the activity label and the heating minutes. Cooling and defrost are teal (`#5ec8c5`). A temporary hold is gold (`#e2c07a`) on the hold label, countdown, and hold pill. The words stay; color repeats them.
- Each room keeps a stable left stripe from its name (slate, tan, or gray). The room name is still the identifier.
- A manual Home, Sleep, or Away profile is labeled in white, not teal, so teal means cooling.

## 0.9.0 — 2026-11-07

- New Climate screen at the ingress panel (`/`). It is the add-on itself, using the existing ingress APIs. The previous dial layout stays at `/classic`. The thermostat card page stays at `/thermostat`. The Lovelace card still works and still links into this panel.
- Schedule preferences are separate from temporary holds. `POST /api/zone/<name>/preference` saves a Home, Sleep, or Away temperature for that room and does not start a hold. `POST /api/zone/<name>/override` with a required `hours` (0.5–12) is the only new way to hold a temperature, and the duration is the one you send. The old `POST /api/zone/<name>/setpoint` path is unchanged and still holds for `override_hours` (2 h) so existing cards keep working.
- The screen shows Full Auto versus a manual Home/Sleep/Away profile, the resolved profile, presence, the reason for the target, and the next schedule change. A temporary hold shows its end time and a Resume automatic control for one room or every room (`POST /api/resume-all`). Opening the screen only reads state.
- Fan Auto is labeled as a fan speed, not as Full Auto.
- History adds humidity when a humidity sensor or climate humidity is already configured, plus a cooling series next to heating and defrost. Runtime minutes are counted from those series. No energy, cost, or efficiency figure is calculated.
- Lovelace default slug is `a1d3bd4b_smart_hvac`. `47b3c4e5_smart_hvac` is still tried if the current slug has no ingress session.
- Security: same ingress-only boundary, no new port, dependency, CDN, telemetry, or privilege. Preference numbers are stored in the add-on’s existing `/data/state.json`. Rollback is the previous add-on version; unknown `prefs` in that file are ignored by 0.8.9.
- Mode lock, defrost, short-cycle, and the spare heater are unchanged.

## 0.8.9 — 2026-11-05

- Heat-pump defrost is on by default (`defrost_aware: true`). A zone whose `hvac_action` is `defrosting` or `preheating` is not a manual change and not a missed setpoint, so the app does not chase the setpoint that cycle. It is not counted as heat delivered. The zone payload includes `defrosting`. The short-cycle guard still blocks a heat/cool flip inside the minimum cycle, including the 10 minutes after a defrost ends (SA M1).
- Heads that never report those actions (only `heating` and `idle`) still get a defrost when `heat_pump: true` (not radiant, not the spare heater): heat mode, known outdoor temperature at or below 40°F (a Celsius weather temperature is converted), and the room or a reported supply temperature falling at least 0.5°F per 3 minutes while heating, or just after heating stops. Cool mode is never inferred. Each window is capped at 15 minutes and 4 per hour. For 10 minutes after a window ends, a same-mode setpoint chase is held, and a mode or setpoint blip in that window is not taken as a new manual hold. The short-cycle guard is not skipped.
- The thermostat card and history graph show a Defrosting badge and a shaded band, including an inferred defrost that never appears in recorder history.
- Set `defrost_aware: false` to restore the previous guards. Mode lock, split logic and the spare bathroom heater are unchanged. No new entities, services or permissions.
- Sources: https://developers.home-assistant.io/blog/2024/07/07/climate-hvacaction-defrosting/ and https://community.home-assistant.io/t/helper-script-to-detect-defrost-status-for-heat-pump/789890

## 0.8.8 — 2026-11-04

- When the unit's own `current_temperature` is used as the room-temperature fallback (sensor unavailable or stale via `indoor_stale_min`), a value outside 30–110 °F (converted from °C when the unit reports `temperature_unit: C`) is treated as missing, so the zone holds (`missing:indoor_temp`) and makes no change.
- New engine-level tests: stale sensor uses the unit temperature; stale plus missing or out-of-range unit temperature holds with no service calls; option off behaves as 0.8.7.
- Follows Security Audit notes L1/L2 on PR #35. Source: https://developers.home-assistant.io/blog/2024/03/20/state_reported_timestamp/
- Mode lock, split logic and spare bathroom heater unchanged; no new options, entities, services or permissions.

## 0.8.7 — 2026-11-04

- New optional zone option `indoor_stale_min` (0–1440, unset/0 = off, so existing installs behave exactly as 0.8.6). A battery room sensor that dies often keeps its last value instead of going `unavailable`; when the zone's `indoor` sensor has not reported for that many minutes (HA `last_reported`, which moves on every write even when the value repeats, else `last_updated`), the add-on ignores it and uses the unit's own `current_temperature`, the same fallback as an unavailable sensor. No timestamp = not stale.
- Source: Home Assistant developer blog, `State.last_reported` (https://developers.home-assistant.io/blog/2024/03/20/state_reported_timestamp/).
- Version 0.8.7. Mode lock, split logic and spare bathroom heater unchanged; no new entities, services or permissions.

## 0.8.6 — 2026-11-03

- Fix: split setpoint writes failed with HTTP 400 every cycle. The `set_temperature` data is now built from the target entity's own attributes: `supported_features` bit 1 (single setpoint) sends `temperature` only; a range-only entity (bit 2) sends `target_temp_low` (heat) or `target_temp_high` (cool/dry) and keeps the other bound at its current value, or one step inside `min_temp`/`max_temp` when it is unknown (an edge value like 88 °F becomes 31.11 °C and fails a device whose native max is 31.0 °C). `hvac_mode` is never sent with a setpoint.
- The setpoint is clamped to `min_temp`/`max_temp` and rounded (half up) to `target_temp_step`. A write that would not change the entity's current setpoint is skipped (counts as applied).
- New optional zone option `setpoint_climate`: the climate entity that receives setpoint writes (default: the zone's `climate`). Use it when a wrapper/proxy climate entity cannot forward a setpoint; modes, reads and the shared-mode lock still use `climate`. Card setpoint writes use the same path.
- Version 0.8.6. Split mode logic, mode lock and spare bathroom heater unchanged.

## 0.8.5 — 2026-11-03

- Heater safety (SA M1b): every in-process heater duration (2 h max run, satisfied stop / 5 min min on-off, 8 h in 24 h lockout, 45 min no-rise, 20 min cooldown) now runs on `time.monotonic()`, anchored to the wall clock once at startup. A wall clock jump mid-run can no longer stretch or shorten any timer.
- Clock-jump guard: every cycle, if the wall clock went backward by more than 60 s, or its step differs from the monotonic step by more than 60 s (either direction), the plug turns OFF, a 20 min cooldown starts (reason `clock_jump`) and a phone alert is sent.
- Persisted `/data/heater.json` timestamps are still sanitized on load as in 0.8.4. Sensor staleness and the watchdog heartbeat still use wall time (they are compared with HA timestamps).
- Version 0.8.5. Split logic and mode lock unchanged.

## 0.8.4 — 2026-11-03

- Heater safety (SA R1): the safety state (no-rise / lockout / relay-fault latch, hard-cap latch, cooldown end, progress checkpoint, current run start, last off time, 24 h run history) is now persisted in `/data/heater.json` with an atomic write (tmp + fsync + `os.replace`) on every change and restored on load. A restart no longer re-arms a latched heater, cuts a cooldown short or forgets run time. Missing file = first run. Unreadable / corrupt file = fail safe: plug off and a 20 min cooldown; a latch is restored only if the file says so.
- Heater safety (SA L1): reaching the setpoint resets the 45 min progress checkpoint once, on crossing, and no longer exempts the run. A reading frozen at or above the setpoint while the plug is on trips `no_rise` after 45 min instead of running a blind 2 h.
- SA follow-ups (0.8.4): persisted timestamps are sanitized on load (anything over 60 s in the future or invalid is dropped, runs with end < start dropped, a cooldown further out than 20 min is clamped to now + 20 min); 24 h run totals never go negative after a backward clock jump, and lockout is still enforced.
- At or above the setpoint a frozen reading no longer latches `no_rise`: the plug goes plain off (`holding`, no latch) and heats again normally when the room drops. Below the setpoint, `no_rise` still latches.
- The current run start is persisted: a restart (or crash loop) within the 20 min window continues the same run for the 2 h max-run timer; a restart after 2 h of running starts the 20 min cooldown.
- Corrupt state file: plug off, 20 min cooldown and a phone alert. The directory is fsynced after the atomic replace.
- Version 0.8.4. Split logic and mode lock unchanged.

## 0.8.3 — 2026-11-03

- Heater safety (SA M2): the no-rise check now uses a progress checkpoint. While the plug is on, the room must rise a further 1°F (from the last checkpoint) or reach the setpoint every 45 min; each 1°F rise resets the checkpoint. A reading that rises once and then sticks (HA reports a repeated identical value as fresh) now trips `no_rise` 45 min after the last rise: plug off, alert, latched (reset with Off then Heat), same as before.

## 0.8.2 — 2026-11-03

- Heater: new `spare_bathroom.stale_minutes` (default 75, schema 15–120) replaces the fixed 15 min stale cutoff. The Zooz ZSE11 reports only hourly or on a 1°F change, so 15 min failed the heater off between normal reports. Freshness = the temp entity's own `last_reported` / `last_updated` (authority). The optional `spare_bathroom.last_seen_sensor` (ISO timestamp state, e.g. Z-Wave JS "Last seen") can only tighten it, never make a reading fresher. Timestamps must be timezone-aware and no more than 60 s in the future; naive, future or unavailable values are ignored (a bad temp timestamp = stale = off). All other safety items unchanged.
- Card: the spare bathroom heater card no longer renders inside every `custom:smart-hvac-card`. It shows only with `show_heater: true` (default false) or the new `layout: heater` (heater card only, no zones). Existing configs keep working; add one of those to the card that should show the heater.
- Version 0.8.2. Split logic and mode lock unchanged.

## 0.8.1 — 2026-11-03

- Fix: a failed `set_temperature` write no longer aborts the whole control cycle. It is wrapped like the mode write: the zone is marked `setpoint_write_failed`, the other zones finish, and state is saved. Mode-lock / split opposite-mode logic is unchanged.
- HA client logs `HTTP <code>` with the method and path (query dropped) on HTTP errors. Never the body or headers.
- Heater-only gates `spare_bathroom.enabled` (default `false`) and `spare_bathroom.dry_run` (default `true`), independent of the global `enabled` / `dry_run`. The plug can turn ON only when the global gate, the heater gate and both entity ids allow it. OFF is always allowed.

## 0.8.0 — 2026-11-03

- New, optional **spare bathroom heater** zone (kitten room): smart plug + plain space heater + temperature sensor, published as `climate.spare_bathroom` (heat / off). Disabled until `spare_bathroom.heater_switch` and `spare_bathroom.temp_sensor` are set. Fully independent of the mini splits: it never reads or writes any `climate.*` entity and is never part of the shared-mode group. No presence, eco or away logic: holds 68°F 24/7; manual card changes 55–75°F.
- Control: on at setpoint −1°F, off at +0.5°F, 5 min minimum on and off; own 30 s loop. Turning on needs `enabled: true` and `dry_run: false`; turning off is always allowed.
- Safety (in add-on code, each with a phone alert): 80°F hard cap latched until below setpoint; 2 h max run then 20 min cooldown; >8 h in 24 h lockout (reset: Off then Heat); sensor unavailable / unknown / stale 15 min → off; no 1°F rise after 45 min → off (latched); plug on when it should be off → forced off; power draw while off → relay fault (latched); door/window open → paused; plug off at startup, shutdown and on any exception.
- Low-temp alerts (configurable): warning below 62°F for 10 min; critical below 58°F, repeating every 30 min until recovered. Alerts name any active lockout / fail-off cause.
- Optional `motion_sensor` is display only (last motion on the card); it never changes heating.
- Lovelace card: the heater shows as its own card (current, target, Heat/Off, status, runtime today, last motion).
- HA client: three narrow new write paths (switch on/off for a `switch.*` id, notification, mirror state for `climate.spare_bathroom` only). Split logic unchanged.
- DOCS: watchdog automation that turns the plug off if the add-on stops updating.
- DOCS: **heater preflight checklist (REQUIRED)**. The heater must not go live (`dry_run: false`) without both (a) the DOCS watchdog automation installed and tested and (b) an on-device auto-off timer of 2 h or less on the smart plug that works without HA (SA M1). Docs only; no code change.

## 0.7.8 — 2026-11-02

- Remove '(new)' name suffix; no functional change.

## 0.7.7 — 2026-11-02

- Shipped sleep/wake defaults are now generic (22:00 / 07:00) in `config.yaml` and the internal `core.DEFAULTS` fallback; saved options are unchanged. The engine reads `profiles.sleep_start` / `profiles.wake` from the Configuration tab and only falls back to the defaults when they are blank. Tests and the golden scenario fixture follow the new defaults (only the reported next-change times move). No control-logic change; split same-mode behavior unchanged.

## 0.7.6 — 2026-11-01

- Add-on lint: removed the obsolete `watchdog` key from `config.yaml` (linter: "'watchdog' is obsolete, use the native Docker HEALTHCHECK"). The Dockerfile `HEALTHCHECK` already probes the same endpoint and port (`http://127.0.0.1:8099/api/health`, python3 urllib; interval 30s, timeout 10s, start period 30s, retries 3). The Watchdog toggle in the Info tab no longer applies. No control-logic change; split same-mode tests unchanged.

## 0.7.5 — 2026-11-01

- New test: with `learned_preheat: true` and `away_delay_min: 15` both on, a fuzz over random start modes, temperatures, overrides, presence flaps and clock steps never requests or leaves the two splits in opposite modes. No control-logic change; both features are option-only and stay off by default.

## 0.7.4 — 2026-11-01

- Public-repo privacy: the Lovelace card's built-in default zones, the card mock page and the thermostat page's offline mock data now use generic placeholders (Room A / Room B / Bath floor, `climate.room_a_mini_split` …) instead of real entity IDs and room names. Set `zones` in the card config to your own entities. Tests, comments and the internal presence key (`owner_home`) no longer name people. No control-logic change; split same-mode tests unchanged.
- Health access-log filter anchored: only a successful (2xx) `GET /api/health` request line is dropped. Errors, other methods, query strings and paths like `/api/healthz` still log (new test).

## 0.7.3 — 2026-11-01

- Supervisor watchdog URL: `watchdog: http://[HOST]:[PORT:8099]/api/health`. With the Watchdog toggle on, the Supervisor restarts the add-on if it stops answering. `/api/health` does not touch the engine or the HA API, so it stays up during startup, a control cycle or an HA outage (new tests). The watchdog check comes from the Supervisor (172.30.32.2), already allowed by the ingress-only filter. No control-logic change.

## 0.7.2 — 2026-11-01

- Public-safe defaults: `zones` ships empty (`[]`); no real entity IDs in `config.yaml`. A generic example is in DOCS. Existing installs keep their saved zones. No engine change.

## 0.7.1 — 2026-11-01

- Logs: the 30-second health check no longer writes an access line (about 2,880 lines a day); all other requests still log. No behaviour change.

## 0.7.0 — 2026-11-01

- Public store marker: name suffix (new); no functional change.

## 0.6.6
- Public-safe defaults: `presence_entities` ships empty (`[]`), `presence_entity` is no longer a default and is now optional in the schema, and `weather_entity` defaults to `""` (schema `str?`). Set them in the Configuration tab. No engine change: blank presence/weather were already ignored. Existing installs keep their saved options.
- Lovelace card: `weather` has no house default (set it in the card config). Docs, changelog and tests use generic names (person_a, person_b, person_c).

## 0.6.5
- One glanceable card: `layout: glance` on `custom:smart-hvac-card` (opt-in; the default layout is unchanged). One row per room: temperature now, -/+ target, and what it's doing (Heating / Idle / Warming up / "Hold · auto at 9:41 PM"). A shared Heat / Cool / Off control for both mini splits (sent to each split in turn, not one call; the add-on turns a split running the other direction off first, so the pair on one outdoor unit is never heat and cool at once; the radiant floor is never included and `dry`/`fan_only` are not offered), plus Auto / Sleep / Away. Header buttons are at least 48 px (Echo Show touch target). Any change is a normal hold that goes back to automatic after `override_hours` (2 h live); an Auto button ends it early. No history fetch in this layout. No add-on behaviour change.

## 0.6.4
- Forecast anticipation (`forecast_anticipation`, new option, **off by default** = 0.6.3 behaviour; off never asks for a forecast). When on, the add-on reads the hourly forecast of `weather_entity` through `weather.get_forecasts` (read-only, return_response; a new `HAClient.get_forecast` that accepts only `weather.*` entities; the climate service allowlist is unchanged), cached 30 min, failures ignored. It uses it to: blend the next-6 h mean into the outdoor temp when no `forecast_entity` is set; feed the next-12 h high to the season guard; and size the learned warm-up (0.6.1) for the forecast temperature at wake time instead of the temperature now.

## 0.6.3
- Hardening (MA #46): a corrupt or unusable learner state (warm-up, coast) is treated as that option off for the cycle and logged once; it can no longer throw before the shared-mode lock runs. `sleep_advance` returns 0 when no rate can be learned; corrupt run records are dropped instead of raising KeyError.
- Sleep coast (`sleep_coast_max_min`, new option, 0-180, **0 by default** = 0.6.2 behaviour, no state kept). When set, each split learns its passive drift rate (unit in heat or cool but idle, room at least 0.5°F past the setpoint, runs of 30+ min and 1°F+) and switches to the sleep target up to that many minutes before `sleep_start`, so the room has drifted down by bedtime. Needs 3 runs; the shared-mode lock and season guard still pick the mode. Evidence: 31 Oct the rooms fell only 73.9 -> 70.8 in 7 h (about 0.4°F/h at 53-58°F outside), so the 68 sleep target was never reached overnight.
- `/api/state` zone results gain `sleep_advance_min` when on.

## 0.6.2
- Presence departure debounce (`away_delay_min`, new option, 0-120, **0 by default** = 0.6.1 behaviour, no state kept). When set (suggested 15): arriving home counts at once; the house only counts as away after every readable phone has been away that long without a break. A tracker that is `unavailable` is ignored (as before), and if no phone is readable the app still holds. Evidence: person_b's iPhone (device_tracker.person_b_phone) flipped unavailable / not_home every few seconds on 28 Oct around 1 AM. Occupants stay person_a + person_b via `presence_entities`; another phone (e.g. person_c's, not linked yet) is just one more list entry.

## 0.6.1
- Learned warm-up (`learned_preheat`, new option, **off by default**; off = byte-identical to 0.6.0, proven by the defrost golden test). When on, each split zone records its own completed recovery runs (unit actively heating or cooling toward a setpoint at least 1°F away, finished within 0.5°F, at least 15 min and 1°F of change) as °F per hour against the outdoor temp at the start, keeping the last 20 per mode. The pre-wake lead becomes gap ÷ learned rate (+10% and 5 min margin), clamped to 15-120 min, with a linear fit on outdoor temp (never extrapolated more than 5°F past what was seen). Fewer than 3 runs = the old fixed 45 min. The radiant floor is not included. Evidence: 31 Oct at 53°F outside, the fixed 45 min lead started at 5:45 AM and the living room reached 71 only around 8:30 AM.
- `/api/state` zone results gain `preheat_lead_min` when the option is on. A wrong-shape `warmup` state entry is reset; removed zones are pruned.

## 0.6.0
- Shared-mode lock (hard rule): both mini splits run off one outdoor unit, so heat and cool are never requested at the same time. dry counts as cool. off is always allowed. fan_only is passed through, but it is not vendor-confirmed for this outdoor unit (see DOCS).
  - Engine: every cycle decides all zones first, then `core.resolve_shared_mode` picks one direction for the split group before anything is written. Precedence: a split the app cannot write (disabled / excluded) > a person's hold (newest change wins if two holds disagree, e.g. Siri on the raw entity) > the direction already running > heat. A zone forced to the group direction reports `reason: mode_lock` and is written even through a manual hold. The radiant floor is not in the group. `/api/state` gains `shared_mode`. The state file shape is unchanged (defrost golden test still byte-identical).
  - API: `/api/zone/<split>/mode` heat or cool also switches every other running split; splits that are off stay off and remember the mode for their next "on". `/api/zone/<split>/power` on joins the direction the other split is running.
  - Card: split Heat / Cool buttons read "Heat · both" / "Cool · both". The card still writes only through the add-on.
  - MA re-gate (#43 CONDITIONAL): `auto` / `heat_cool` count as a conflict beside any other running split and are forced to the group's heat / cool (never chosen as the group direction). Direction changes write the other split `off` first, then the new mode, so no write ever leaves heat + cool live (engine and `/mode`, `/power`). A failed write never leaves opposites: the engine only writes a mode that is consistent with what is live and stops a writable split if failures left a conflict; `/mode` / `/power` roll the other split back and return 502 (not 500). Card writes now take the same lock as the engine cycle. MA's adversarial suite is part of the tests.
  - Tests: exhaustive resolver check (all want / live / hold combinations), 1,500-case engine fuzz over 3 cycles, every 3-tap card sequence through the API, and scenario tests for each audited path.

## 0.5.1
- `defrost_aware` (new option, **off by default**; off = byte-identical to 0.5.0, proven by a golden-output test). When on (MA design review 30 Oct, all four conditions):
  - Every defrost exemption window (reported or inferred) is capped at 15 min, and at most 4 windows may start in any 60 min. Past either limit the normal short-cycle / manual-hold / missed-setpoint logic resumes, so a failing unit is never masked; past the cap the existing `defrost_stuck` flag is set.
  - The official `hvac_action` `defrosting` / `preheating` (HA dev blog 2024-07-07) is always honoured, on any zone.
  - Fallback inference only on zones flagged with the new zone option `heat_pump: true`, in heat mode, while the unit reports `heating`, with a known outdoor temp at or below 40°F, when the room temp falls at least 0.5°F per 3 min (HA community thread 789890). Unknown / unavailable outdoor temp = no inference. Zone payload gains `defrost_inferred`.
  - Debug logs only: no new entities, HA writes, services or permissions. A few room-temp samples per zone are kept in the add-on's own state file (pruned with removed zones).
- MA L1 (PR review 30 Oct): a state file that parses but has the wrong shape no longer errors every cycle. A non-object top level is ignored; a top-level key of the wrong type, a malformed zone entry (incl. `last_result.zones`), and malformed defrost samples / episodes are reset with a debug log. Well-formed state is untouched.

## 0.5.0
- Window delay + restore (per zone, contact sensor only, no slope detection; pattern from Versatile Thermostat's sensor mode): new zone options `window_action` (`off` default / `fan_only` / `eco`), `window_open_delay_s`, `window_close_delay_s` (0-3600 s, default 0). A window must stay open for the delay before the app acts; the zone's mode + setpoint are saved and restored once after it stays closed for the close delay. The saved state is discarded (never restored) if vacation starts, or anyone changes the zone's mode or setpoint (incl. turning it off), after the window opened. Zones without a `window` sensor are unchanged (none configured live). Defaults keep pre-0.5.0 behaviour (immediate off). MA L1: a restore never writes directly; the saved setpoint is fed to the decision core as the desired target only when the saved mode equals the mode it picks now (else dropped), and is clamped like any target (min/max, `radiant_setpoint_max`), so `cool_lockout` and the vacation setback still apply. `window_action: off` (default) saves and restores nothing, i.e. exactly the 0.4.1 behaviour. MA L3: `window` must be a `binary_sensor.*` entity. MA L4: removing a zone's `window` sensor drops its saved window state.

## 0.4.1
- MA L1: a unit that keeps reporting `defrosting` / `preheating` for more than 30 minutes in one spell no longer freezes the app: normal logic resumes (a change seen then counts as manual again) and a `defrost_stuck` event is recorded (red on the card's event strip) until the unit stops reporting it.
- Season guard (found live 30 Oct: projection showed "cool 67.5° at 10:32 PM" for a 71° room at the 68° sleep target with 58° outside): when the outdoor temp / forecast high is below `cool_lockout` (new option, default 75°F) the splits never switch to cool; a room above target coasts in heat (unit idles). Above the lockout the old deadband logic is unchanged. No summer heat lockout (not needed for this fix).
- Next change projection: the room temp now relaxes toward the setpoint in force (90-min time constant) instead of being held at the current reading.
- MA L2: at start, stored events / active kinds / defrost timers of zones no longer in the options are dropped (logged).

## 0.4.0
- Defrost awareness: `hvac_action` `defrosting` / `preheating` is not a manual change, not a short cycle and not a missed setpoint (no write that cycle); card shows "Defrosting" and a graph band. Live check 30 Oct: the splits (raw ESPHome + `_proxy`) never reported either and have no defrost attribute, so it is display-on-report only, no inference.
- Card: one-tap Home / Sleep / Away / Eco presets via the new `/api/preset` (ingress, MA M1 pattern; Eco = automatic); "next change / why" line (e.g. "71° at 7:00 AM · pre-heat 6:15 AM"); 24 h event strip (hold, window, vacation, defrost, pre-heat); still fits the Echo Show at 1280x800.
- MA lows from 0.3.3: `/api/state` `radiant_room_cap` documented as a deprecated alias of `radiant_air_shutoff`; a warning is logged at start when the ignored legacy `radiant_room_cap` is set lower than `radiant_setpoint_max`; the card reads `radiant_setpoint_max` from `/api/state` instead of a hard 85 (still never above 85).

## 0.3.3
- A reported setpoint within 1°F of the app's last write for that zone is treated as the app's own write, not a manual change (found live 30 Oct: a split rounded the vacation setback 60 -> 61 and the app held it as a manual override). A change of more than 1°F in HA still starts a hold; tolerance is inclusive because the units round by exactly 1°F (trade-off documented in DOCS).
- Leaving vacation restores the scheduled setpoint even when the unit rounded the setback; while on vacation the rounded setback is not re-sent every cycle.
- Radiant cap split (owner, 30 Oct): `radiant_setpoint_max` (floor setpoint cap, default 85) and `radiant_air_shutoff` (bath air shutoff, default 80), both hard-capped at 85 in the engine and server; the Lovelace card clamps setpoints at 85. Defaults apply with no option change at install; the old `radiant_room_cap` option (saved as 80 by 0.3.2) is accepted by the schema but ignored. `/api/state` adds `radiant_setpoint_max` / `radiant_air_shutoff` (`radiant_room_cap` = air shutoff, for compatibility); the panel shows both.

## 0.3.2
- Card writes show right away: the reply to setpoint / mode / fan / power (and `/api/state`) reflects the written value immediately, instead of the pre-write read for up to 5 minutes.
- Resume auto restores the scheduled setpoint in the same request and no longer re-holds because of the card's own earlier write (found live 30 Oct). A person's change in HA still starts a hold.
- Vacation mode: follows `input_boolean.vacation_mode` (the helper `vacation_mode_handler` reads). Mini splits set back to `vacation_heat` (60) / `vacation_cool` (82); the radiant stays off (app turns it off if on; a person's off is left alone; cards can't turn it on, 409); radiant cap stays 80. Manual holds and M1 still respected. Cards show "Vacation" and why. `vacation_mode_handler` is not touched.
- Native Lovelace card `custom:smart-hvac-card` (`lovelace/smart-hvac-card.js`): same ecobee-style cards from hass state + recorder history, no iframe, 1280x800 Echo Show layout. Writes are POSTed to the add-on API through Supervisor ingress (admin users), so the add-on's gates and the 80 radiant cap apply; the card never calls climate services itself. Non-admin users get a clearly labelled view-only card. Registered manually (see DOCS).
- Climate panel shows the vacation reasons.
- CI: Node test step for the card.

## 0.3.1
- History graph fixes found in live verification of 0.3.0: use `last_updated` (attribute changes: setpoint, hvac_action, weather temp) instead of `last_changed`; clamp the carried-in first row to the window start; sort by time only (an off/None setpoint at the same timestamp could raise); a unit that is off is never drawn as heating (radiant reports stale `hvac_action: heating` while off); indoor/outdoor lines carry the last value to now.
- Card state: after the app writes, the card shows the written setpoint/mode instead of the pre-write read.

## 0.3.0
- Thermostat cards (Ingress page `thermostat`, linked from the Climate panel): one dark face per zone (living room + bedroom mini splits, bath radiant) with big current temp, setpoint pill and -/+, mode icon, weather icon, humidity + status, menu for mode / fan / power.
- Smart HVAC state on each card: active profile, app-in-control vs manual hold (with expiry), reason for the setpoint, outdoor temp, radiant 90-min cap + room cap.
- Every card write starts a manual hold the engine already respects; per-zone "Resume auto" and "Hold Nh". Cards never write schedules.
- History graph per card (24h / 7d, outdoor overlay): indoor, setpoint step line, heating on/off band, read-only from HA `/history/period`.
- API: POST zone/{z}/mode, /power, /fan, /hold (+ existing /setpoint, /resume); GET zone/{z}/history?range=24h|7d. JSON-only, 400 on bad body, 409 in dry run/disabled.
- HA client allowlist: adds `climate.set_fan_mode` only. On/off uses `set_hvac_mode`; `turn_on/turn_off` stay blocked. Radiant setpoint capped at `radiant_room_cap`.

## 0.2.0
- Multi-person occupancy: `presence_entities` (any home = occupied). person_a + person_b (`device_tracker.person_b_phone`); person_c slot unset until their tracker is known. Fail-safe hold only if no occupant is readable.

## 0.1.0
- Initial add-on: port of PR #36 core (M1 manual-off hold, radiant over-temp, anti-short-cycle, fail-safe hold, override auto-resume) + Ingress GUI.
