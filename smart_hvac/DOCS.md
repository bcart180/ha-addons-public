# Smart HVAC

A smart thermostat built for multi-zone mini-split systems. All indoor heads share one outdoor unit, so the
add-on always keeps them in the same heat/cool mode: never one heating while another cools.

## Safe by default
Installs with **enabled: false** and **dry_run: true**. In that state it reads sensors every 5 minutes and shows
what it *would* do in the Climate panel, but sends nothing to any thermostat. Go-live = set `enabled: true`,
`dry_run: false` after Mission Assurance review and a CoS GO. Pick **Guest** in the panel first if the owner is away
and guests are home (away would set back to 65°F).

## Install
Settings → Apps → App Store → ⋮ → Repositories → `https://github.com/bcart180/ha-addons-public` → Smart HVAC → Install.
Do not Start until the go-live checklist below is done; Start alone is still dry-run.

## Options
- `presence_entity` + `presence_entities`: occupants (any home = occupied). List each occupant's tracker, e.g. `device_tracker.person_a_phone`, `person.person_b`. Empty by default. Unlisted phones never count.
- `zones[]`: `climate`, `indoor`, `humidity`, optional `floor` (radiant probe) and `window` (contact sensor),
  `indoor_stale_min` (0.8.7, optional, 0/unset = off): if the zone's `indoor` sensor has not reported for this many minutes
  (HA `last_reported`, else `last_updated`), it is ignored and the unit's own `current_temperature` is used, the same as when the sensor is unavailable. Since 0.8.8 a unit reading outside 30–110 °F (°C converted) counts as missing, so the zone holds.
  `window_action` (`off` default / `fan_only` / `eco`), `window_open_delay_s` / `window_close_delay_s` (0.5.0, default 0),
  `facing_az`/`solar_factor` (sun gain), `heat_only`, and per-zone `comfort`/`sleep`/`away` (radiant).
- Example `zones` (generic names; replace with your own entities):
  ```yaml
  zones:
    - name: room_a
      label: Room A
      climate: climate.room_a_mini_split
      indoor: sensor.room_a_temperature
      humidity: sensor.room_a_humidity
      facing_az: 180
      solar_factor: 0.5
      heat_only: false
      enabled: true
    - name: bath_floor
      label: Bath floor
      climate: climate.bath_floor
      indoor: sensor.bath_temperature
      facing_az: 90
      solar_factor: 0
      heat_only: true
      enabled: true
      comfort: 72
      sleep: 66
      away: 60
  ```
  `zones` ships empty (0.7.2): with no zones the add-on starts, shows nothing to control and sends nothing.
  Existing installs keep their saved zones.
- `setpoint_climate` (0.8.6, optional, per zone): climate entity that receives `set_temperature` (default: the zone's `climate`). Setpoint writes follow that entity's `supported_features` (single `temperature` vs `target_temp_low`/`target_temp_high`), are clamped to its `min_temp`/`max_temp`, rounded to its `target_temp_step`, and skipped when unchanged. Modes and the shared-mode lock always use `climate`. See the HA climate entity docs (https://developers.home-assistant.io/docs/core/entity/climate/).
- `profiles`: home/guest 71, sleep 68, away 65/78, sleep 22:00–07:00.
- `min_cycle_min` (anti-short-cycle), `override_hours` (auto-resume), `radiant_setpoint_max` (highest bath floor setpoint a card / panel may set, default 85), `radiant_air_shutoff` (bath air above this turns the radiant off, default 80), both capped at 85; `radiant_floor_cap`. `cool_lockout` (0.4.1, default 75): while the outdoor temp or forecast high is below this the splits never switch to cool, a warm room just coasts in heat. The old `radiant_room_cap` option is still accepted but ignored; if it is set lower than `radiant_setpoint_max` (0.3.2 saved 80) the add-on logs a warning at start saying so. In `/api/state`, `radiant_room_cap` is a **deprecated alias** of `radiant_air_shutoff` kept for 0.3.x clients; the panel and card read `radiant_setpoint_max` / `radiant_air_shutoff`.

## Behaviour
- **Open window from temperature slope (0.9.5)**: `window_slope` (default **on**). Detection uses each mini split that has an `indoor` sensor and no contact `window` sensor. The pause and the resume still apply to every mini split the app controls, including a partner that has a contact sensor or no room sensor. Radiant (`heat_only`) zones are excluded. Set `window_slope: false` to match 0.9.4.
  The signal is only that room sensor, as samples of `(last_changed, value)`. The slope is `(temperature now − temperature held 20 minutes ago) / 20 minutes`, in °F/hr. It is undefined until the sensor history covers the lookback, so a gap (unavailable, non-numeric, or stale via `indoor_stale_min`) does not compare across the gap. The unit's `current_temperature` is never used, including the stale fallback.
  These sensors often report only when the value changes by about 1°F, with long gaps between reports. A short moving average turns one 1°F step into a false steep slope. The 20-minute difference does not: one 1°F step is about 3°F/hr. A real fall of 6°F/hr, 10°F/hr, or 20°F/hr still crosses 5.4°F/hr; a 3°F/hr drift does not.
  Every controlled mini split pauses together, and only while the triggering zone's mode is heat, the master switch is on, and it is not vacation. The fall must stay steeper than the threshold for the delay. No pause within 45 minutes of a setpoint drop or a mode change, none during defrost or the grace after it, none unless outdoor air is known and at least 5°F colder than the room, and at most 2 pauses for the pair in any hour. The pause writes through the zone's `window_action` (default off) and is the app's own write, so the next cycle does not start a hold. A split a person already turned off stays off. They resume together when the triggering zone's slope is flat or rising, or after `window_slope_max_min`, whichever comes first. After a max-duration resume the detector waits until the slope is flat or rising before it can pause again. Resume goes through the normal decision, so clamps, cool lockout, vacation, and the shared-mode lock still apply.
  A manual hold pauses with its partner. The hold timer and the remembered override temperature are not cleared. A person who changes a split during the pause is honored; that split's saved state is dropped. The pause is stored in the add-on state file. After a restart the paused splits are still the app's off, so they can resume. A pause that has already run past its max resumes on the first cycle.
  Per zone (all optional): `window_slope_threshold_f_per_h` (2–15, default 5.4), `window_slope_delay_s` (0–1800, default 120), `window_slope_max_min` (5–120, default 30), `window_slope_lookback_min` (10–60, default 20). While paused the reason is `window_slope_pause` ("Paused, window open?") on both splits. `/api/state` adds `window_slope`: `{active, since, max_until, slope_f_per_h, trigger_zone}` with numbers or null.
  Rollback: if a pause is active, let it resume, or set both splits back to their prior mode, before installing 0.9.4. 0.9.4 treats every off as a person's off after a restart and will not turn the splits back on. Set `window_slope: false` and remove the per-zone `window_slope_*` keys first; 0.9.4's schema does not know them. 0.9.4 keeps the new state-file keys but never reads them.
- **Window delay + restore (0.5.0, contact sensor only, no slope detection)**: only for a zone with a `window`
  sensor (none configured live today, so nothing changes at install). The sensor must stay open for
  `window_open_delay_s` before the app acts, then it saves the zone's mode + setpoint and applies `window_action`:
  `off` (default, same as before 0.5.0), `fan_only` (falls back to off if the unit does not list it) or `eco`
  (keeps heat/cool, away setback). After it stays closed for `window_close_delay_s` the saved mode + setpoint are
  restored once (reason `window_restore`) through the normal decision (only if the saved mode is the mode the
  app would pick now; clamped to min/max and `radiant_setpoint_max`). `off` saves/restores nothing (0.4.1 behaviour). The saved state is discarded if vacation starts or anyone changes
  the zone's mode or setpoint (incl. off) after the window opened.
  An unreadable sensor keeps the last confirmed state. Pattern: https://www.versatile-thermostat.org/en/docs/feature-window/
- Missing input → hold last setpoint. Unit off by a person → stays off (M1). Radiant over cap → off, stays off.
- Change a setpoint in HA or the panel → manual override for `override_hours`, then back to auto.
- Own-write tolerance (0.3.3): a reported setpoint within **1.0°F (inclusive)** of the add-on's last write for that zone
  counts as the add-on's own write, not a manual change. Reason: the mini splits round writes by exactly 1°F
  (observed live 30 Oct: wrote 60, unit reported 61). Trade-off: a person's exact 1°F nudge in HA is not treated as a
  hold (the add-on keeps its schedule); a change of 2°F or more still starts a hold. Card / panel writes are always
  holds, since they go through the add-on.

## Go-live checklist
1. Pause the old HVAC automations (family leaves/arrives, vacation, cleaning, radiant) — CoS GO.
2. Panel → Guest (if needed). 3. Watch one dry-run cycle in the panel. 4. `dry_run: false`, `enabled: true`.
5. Verify both mini splits still read 71°F after one cycle.

## Thermostat cards
`<ingress>/thermostat` still opens the thermostat card page. The Climate screen does not link to it. Each zone shows current temp, setpoint
(-/+), mode, fan and power in the ☰ menu, and the app's state: profile, "App in control" or "Manual hold until …",
the reason, outdoor temp, and for the bath floor the 90-min cap automation state and the room cap.
Any card change = manual hold for `override_hours` (default 2). **Resume auto** hands the zone back immediately.
Cards are read-only when the app is off or in dry run. The graph shows indoor (white), setpoint (teal step),
heating (amber band) and outdoor (dashed), 24h or 7d.
Note: turning a unit **off** from the card is a person's off, so the app will not turn it back on (M1) — use **On**.

## Vacation mode (0.3.2)
Turn on `input_boolean.vacation_mode`. Every cycle: mini splits go to `vacation_heat` (default 60°F) or
`vacation_cool` (82°F) in cooling season; the bath radiant is turned off and stays off (cards can't turn it on).
Units a person turned off stay off. Turn the helper off and the normal schedule returns on the next cycle.
The old `vacation_mode_handler` automation is left as-is until vacation mode is verified live.

## Climate screen (0.9.0)
The ingress panel (`/`) is the Climate screen: large temperatures, the schedule, and a history graph. It reads `/api/state` when it opens and does not change a hold or a thermostat by being opened.

Color (0.9.2). The screen stays black, with `#414141` cards and `#757575` for a selected control. Blue `#005aff` marks an active control and the target line. Green `#3ddc5a` is only the current indoor temperature. Heating, cooling, a temporary hold, and defrost are told by their words. There is no amber, gold, or colored left stripe. Each room shows its history graph with the temperature.

The controls:

- **Full Auto** is the global schedule (presence and time of day). Home, Sleep, and Away in that row are a manual profile for the whole house. They are not a room hold. The screen also shows the resolved profile and presence separately. If those disagree, both are shown and neither is cleared.
- **Schedule preference** (`POST /api/zone/<name>/preference`, `{"profile":"home"|"sleep"|"away_heat"|"away_cool","temperature":70}`) saves that room’s target for the schedule. It does not start a hold. A room that is already held keeps the hold; the new preference is used when the hold ends.
- **Override temperature** (`POST /api/zone/<name>/override-temp`, `{"temperature":70}`) remembers one number for that room. It does not start a hold and does not change the unit. There is no heat or cool choice. Until a number is saved, the stepper drafts from the live hold setpoint when a hold is on, otherwise from the current auto setpoint.
- **Temporary hold** (`POST /api/zone/<name>/override`, `temperature` plus required `hours`) writes that setpoint, remembers it, and holds it for that many hours. Heat or cool is chosen from the temperature with the same Full Auto rules. Resume one room with `POST /api/zone/<name>/resume`. Resume every room with `POST /api/resume-all`. The older `POST /api/zone/<name>/setpoint` still starts the default 2 hour hold so existing cards do not change. The remembered number stays after resume, after the timer, and after a restart.

A room with no hold shows `Auto 71°F`. A hold shows `Manual override 70°F until 9:41 PM ET` (America/New_York). Off · both turns both mini splits off. The screen does not offer Hold both in Heat or Hold both in Cool. Fan Auto, when a unit has it, is only a fan speed.

`/classic` and `/thermostat` still open. They are not linked from this screen. The thermostat page and the Lovelace card still have a Heat / Cool control; that is unchanged in this version.

The graph uses recorder history already available to the add-on (indoor, target, outdoor, humidity when present, heating, cooling, defrost). Minutes in view are sums of those series. Gaps are marked and are not filled with a made-up temperature. There is no energy, cost, or efficiency estimate.

`?mock=1` is a labeled synthetic example for screenshots. It does not talk to Home Assistant.

Rollback: install the previous add-on version. 0.9.2 leaves an `override_temps` object in `/data/state.json` unused. It does not rewrite that file. A `prefs` object left in the same file is ignored by 0.8.9. Holds and the Lovelace card keep working. The options schema is the same as 0.9.2.

## Lovelace card (0.3.2)
`custom:smart-hvac-card` shows the thermostat cards natively on any dashboard (Echo Show 1280x800 included),
using HA state and history directly; no iframe, no ingress.

The add-on cannot register a dashboard resource itself: that needs write access to `/config` and an admin
websocket token, which this add-on deliberately does not have. One-time manual setup:
1. Copy `smart_hvac/lovelace/smart-hvac-card.js` from the repo to `/config/www/smart-hvac-card.js`.
2. Settings → Dashboards → ⋮ → Resources → Add: URL `/local/smart-hvac-card.js`, type **JavaScript module**.
3. Add a card: `type: custom:smart-hvac-card` (set `zones` to your own entities; the built-in defaults are generic placeholders, see the example above).

Card writes never call HA services from the browser. Each write is a POST to this add-on's API
(`api/zone/<zone>/setpoint|mode|fan`) through Supervisor ingress, so every add-on rule applies: app off / dry run
(409), manual hold, vacation (radiant can't be turned on, 409), radiant setpoint cap 85 (`radiant_setpoint_max`) with air shutoff 80 (`radiant_air_shutoff`), Tesla exclusion. The card opens the
ingress session with the frontend's `supervisor/ingress/session` command (falls back to the admin-only
`supervisor/api` tunnel on older cores). Because the add-on panel is admin-only, Supervisor only grants that
session to **admin** users. For any non-admin user (for example a wall/Echo Show account) the card is
**view only** and shows a banner linking to the Climate panel; it has no other write path.

## 0.4.0: defrost, presets, next change, events
- **Defrost** (0.8.9, `defrost_aware` default **on**): a heat pump that is defrosting is not a missed setpoint.
  The app does not chase the setpoint that cycle and does not treat the spell as a manual change. It is also
  not counted as heat delivered (learned warm-up and sleep coast skip it). The short-cycle guard still blocks
  a heat/cool flip inside the minimum cycle, including the 10 minutes after a defrost ends.
  Reported: `hvac_action` `defrosting` or `preheating` (any zone). Inferred, because some heads only ever report
  `heating` / `idle`: zones with `heat_pump: true` that are not radiant, in heat mode, outdoor temperature known
  and at or below 40°F (a Celsius weather temperature is converted first), and the room — or a supply temperature
  the climate entity already reports — falling at least 0.5°F per 3 minutes while the unit is heating or has just
  gone idle from heating. Cool mode is never inferred. Unknown outdoor temperature means no inference. Each window
  is at most 15 minutes, and at most 4 windows may start per hour; past either limit normal logic resumes
  (`defrost_stuck` past the cap). For 10 minutes after a window ends, a same-mode setpoint chase is held, and a
  mode or setpoint blip in that window is not taken as a new manual hold. The short-cycle guard is not skipped.
  The zone payload sets `defrosting: true` (and `defrost` / `defrost_inferred`). The card and the history graph
  show a Defrosting badge and a shaded band, including an inferred defrost that is not in recorder history.
  `defrost_aware: false` restores the previous guards. A state file that omits the option still behaves as off,
  which is how the older golden test stays stable; the add-on fills the default when it loads options.
  Sources: https://developers.home-assistant.io/blog/2024/07/07/climate-hvacaction-defrosting/
  and https://community.home-assistant.io/t/helper-script-to-detect-defrost-status-for-heat-pump/789890
  (0.4.1) A reported spell longer than 30 minutes, when `defrost_aware` is off, stops waiting and records
  `defrost_stuck`. With the option on, the cap is 15 minutes.
- **Presets** `POST /api/preset {"preset": "home"|"sleep"|"away"|"eco"}` (ingress only) set the global profile:
  Home/Sleep/Away = those profiles, **Eco = automatic** (presence + schedule: away setback when nobody is home,
  sleep at night). Same effect as `/api/profile`; the cycle runs right away and writes through the usual gates.
- **Next change / why**: each zone in `last.zones` has `next` = `{ts, setpoint, mode, profile, preheat, wake_ts}`,
  projected by running the decision core forward in 5-min steps with today's weather / presence / indoor
  held (sun left out). None while on hold, vacation, defrost, off, or a window is open.
- **Events**: `zones[].events` = `[ts, kind, start|end]` for the last 24 h, kinds `hold`, `window`, `vacation`,
  `defrost`, `preheat`, `defrost_stuck` (kept to the last 200 per zone; zones removed from the options are pruned at start).
- The card reads `/api/state` through ingress (admins) for the radiant cap, next change and events; view-only
  users fall back to 85 and hass state.

## 0.6.0: shared-mode lock (hard rule)
Both mini splits share one outdoor unit, which cannot heat one room while it cools the other. The add-on never requests heat on one split and cool (or dry) on the other:
- The engine decides every zone, then picks one direction for the splits before writing. A forced zone shows `mode_lock` and is written even through a manual hold.
- Card Heat / Cool on a split switches both splits ("Heat · both"). Off stays per room.
- Changes made outside the add-on (HomeKit / Siri on the raw entities) are corrected on the next cycle (newest change wins). Removing the raw entities from the HomeKit bridge closes that path at the source.
- **fan_only is not confirmed safe** for this outdoor unit: the add-on requests it only for `window_action: fan_only` (no zone uses it) and leaves a person's fan_only alone; check the unit manual before relying on it while the other split heats or cools.

## 0.6.1: learned warm-up (option `learned_preheat`, off by default)
Each mini split learns how fast its room really heats (or cools) from its own recovery runs and starts the pre-wake change that many minutes early (15 to 180 min) instead of a fixed 45. Until a room has 3 runs, it uses 45. The current lead shows as `preheat_lead_min` in the add-on state.

## 0.6.2: presence debounce (option `away_delay_min`, 0 = off)
Any phone in `presence_entities` at home = home, at once. Away only after all of them have been away for `away_delay_min` minutes in a row, so a phone dropping off Wi-Fi doesn't trigger a setback. Phones that read `unavailable` are ignored. To count another person later, add their phone tracker to `presence_entities`.

## 0.6.3: sleep coast (option `sleep_coast_max_min`, 0 = off)
The house holds heat well, so a sleep target set at bedtime is rarely reached. With this option each mini split learns how fast its room drifts with the unit idle and lowers to the sleep target early enough to get there by `sleep_start`, at most this many minutes early. Away, guest and manual profiles are unaffected.

## 0.6.4: forecast anticipation (option `forecast_anticipation`, off by default)
Reads the hourly forecast (read-only) so the add-on reacts to the weather that is coming: a colder morning starts the learned warm-up earlier, and a hot afternoon ahead is seen by the season guard. Both splits still share one mode.

## 0.6.5: one glanceable card (`layout: glance`)
```yaml
type: custom:smart-hvac-card
layout: glance
```
One row per room (now, -/+ target, why). The Heat/Cool/Off control sets each mini split in turn, one request after another; the add-on turns a split running the other direction off first, so the pair (one outdoor unit) is never heat and cool at once. Changes are holds that revert to automatic after `override_hours`; tap Auto to resume now.

## Spare bathroom heater (0.8.0)
Kitten room: a smart plug drives a plain space heater from a temperature sensor. Shown as its own card and as
`climate.spare_bathroom` (heat / off; a mirror state the add-on publishes). It is never linked to the mini splits
and has no presence / away / eco logic: it holds the setpoint (68°F default, 55–75°F from the card) all day.

Options (Configuration → `spare_bathroom`; blank = zone disabled):
```yaml
spare_bathroom:
  heater_switch: switch.example_heater_plug        # required
  temp_sensor: sensor.example_spare_bath_temp      # required
  power_sensor: sensor.example_heater_plug_power   # optional: relay-fault check
  door_sensor: binary_sensor.example_bath_door     # optional: pause while open
  notify_service: notify.mobile_app_example_phone  # optional: phone alerts for real faults only
  motion_sensor: binary_sensor.example_bath_motion # optional: display only
  stale_minutes: 75                                # 0.8.2: 15-120; minutes without a report before fail-off
  last_seen_sensor: sensor.example_node_last_seen  # 0.8.2 optional: Z-Wave "last seen" timestamp sensor
  low_warn_f: 62
  low_warn_min: 10
  low_crit_f: 58
  low_crit_repeat_min: 30
```
Safety (fixed in code): 80°F cap (latched until below setpoint), 2 h max run + 20 min cooldown, 8 h / 24 h lockout
(reset with Off then Heat), sensor unavailable or stale (`stale_minutes`, default 75) → off, no further 1°F rise (or setpoint reached) within 45 min of the last checkpoint → off (0.8.3; reaching the setpoint resets the checkpoint once; a frozen reading at/above the setpoint turns the plug plain off (`holding`, no latch), 0.8.4). The no-rise latch clears itself (0.9.6) once a reading the sensor reports after the trip is at least 1°F above the temperature at the last checkpoint: the heater did warm the room and the trip came from a slow-reporting sensor. Without such a reading it stays latched until Off then Heat. From 0.9.7, the trip and the auto-clear are logged only; a persistent notification is sent only if a later post-trip reading still does not show a 1°F rise, or if a legacy latch has no trip info so it cannot auto-clear. The 2 h cooldown is also log-only. Other real faults (stale or missing sensor, 80°F cap, unexpected-on, relay fault, 8 h / 24 h lockout, clock jump, unreadable state, door open, low-temp warning) still notify. Latches, cooldown, checkpoint and 24 h run history persist across restarts in `/data/heater.json` (0.8.4; timestamps sanitized on load; the current run start survives restarts so the 2 h max run cannot be reset by a restart loop; corrupt file → off + 20 min cooldown + alert); heater timers run on a monotonic clock, and a wall clock jump (back > 60 s, or > 60 s off from monotonic elapsed) → off + 20 min cooldown + alert (`clock_jump`, 0.8.5), plug on
unexpectedly → forced off, power draw while off → relay fault, door open → pause, plug off at start/stop/error.
Practice mode never turns the plug on. Since 0.8.1 the heater has its own gates: `spare_bathroom.enabled` (default `false`) and `spare_bathroom.dry_run` (default `true`), independent of the global ones. The plug turns ON only when global `enabled: true` + `dry_run: false` AND `spare_bathroom.enabled: true` + `spare_bathroom.dry_run: false` AND both entity ids are set.

**Stale sensor (0.8.2).** A battery sensor such as the Zooz ZSE11 reports only on a 1°F change or once an hour,
so the cutoff is `stale_minutes` (default 75, range 15–120) instead of a fixed 15 minutes. The temperature
entity's own `last_reported` / `last_updated` decides the age. If `last_seen_sensor` is set (an ISO timestamp; Z-Wave
JS exposes one per node as a disabled-by-default "Last seen" sensor), it can only make the check stricter: an old
last-seen turns the heater off, a recent one never rescues an old reading. Timestamps without a time zone or more
than 60 s in the future are ignored; a bad temperature timestamp counts as stale.

**Card placement (0.8.2).** The heater card shows only on a `custom:smart-hvac-card` that asks for it, so
several cards on one dashboard no longer each repeat it:
```yaml
type: custom:smart-hvac-card
layout: heater          # only the heater card, no zones needed
# or, on an existing card:
show_heater: true       # default false
```

### Heater preflight checklist (REQUIRED before `dry_run: false`)
The add-on cannot turn the plug off if it hangs or Home Assistant is down. **Do not take the heater live
(`dry_run: false`) until both items below are done and checked.** Leave `dry_run: true` until then.
- [ ] **(a) Watchdog automation installed** in Home Assistant (YAML below, with the real plug id), enabled, and
  tested once (stop the add-on with the plug on → plug turns off within ~3 minutes).
- [ ] **(b) On-device auto-off timer set on the smart plug, 2 hours or less**, configured in the plug's own app or
  firmware so it runs on the plug itself and works **without Home Assistant** (test: turn the plug on with HA
  unreachable; it must switch off by itself within 2 hours).

If either item is missing, the heater must stay in practice mode.

### Watchdog automation (REQUIRED before `dry_run: false`; plain HA, outside the add-on)
Turns the plug off if the add-on stops publishing (heartbeat older than 3 minutes) or the add-on is not running.
Replace the example entity ids.
```yaml
alias: Spare bathroom heater watchdog
mode: single
triggers:
  - trigger: time_pattern
    minutes: "/1"
  - trigger: state
    entity_id: binary_sensor.smart_hvac_running
    to: "off"
conditions:
  - condition: state
    entity_id: switch.example_heater_plug
    state: "on"
  - condition: template
    value_template: >
      {% set hb = state_attr('climate.spare_bathroom', 'heartbeat') %}
      {{ hb is none or (as_timestamp(now()) - hb | float(0)) > 180
         or is_state('binary_sensor.smart_hvac_running', 'off') }}
actions:
  - action: switch.turn_off
    target:
      entity_id: switch.example_heater_plug
  - action: persistent_notification.create
    data:
      title: Spare bathroom heater
      message: Smart HVAC is not responding. Watchdog turned the heater plug off.
```
