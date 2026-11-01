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
- Learned warm-up (`learned_preheat`, new option, **off by default**; off = byte-identical to 0.6.0, proven by the defrost golden test). When on, each split zone records its own completed recovery runs (unit actively heating or cooling toward a setpoint at least 1°F away, finished within 0.5°F, at least 15 min and 1°F of change) as °F per hour against the outdoor temp at the start, keeping the last 20 per mode. The pre-wake lead becomes gap ÷ learned rate (+10% and 5 min margin), clamped to 15-180 min, with a linear fit on outdoor temp (never extrapolated more than 5°F past what was seen). Fewer than 3 runs = the old fixed 45 min. The radiant floor is not included. Evidence: 31 Oct at 53°F outside, the fixed 45 min lead started at 5:45 AM and the living room reached 71 only around 8:30 AM.
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
- Card: one-tap Home / Sleep / Away / Eco presets via the new `/api/preset` (ingress, MA M1 pattern; Eco = automatic); "next change / why" line (e.g. "71° at 6:30 AM · pre-heat 5:45 AM"); 24 h event strip (hold, window, vacation, defrost, pre-heat); still fits the Echo Show at 1280x800.
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
