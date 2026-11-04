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
  (HA `last_reported`, else `last_updated`), it is ignored and the unit's own `current_temperature` is used, the same as when the sensor is unavailable.
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
Open the Climate panel and tap **Thermostat cards** (or `<ingress>/thermostat`). Each zone shows current temp, setpoint
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
- **Defrost**: when a unit reports Home Assistant's standard `hvac_action` `defrosting` or `preheating`, the app
  does not treat a setpoint/mode it sees as a manual change, does not count it as a mode change for the
  anti-short-cycle guard, and does not write (no "missed setpoint" retry). The zone reason is `defrosting` /
  `preheating`; cards show "Defrosting" / "Pre-heating" and a band on the graph. Checked live 30 Oct: the
  two Mitsubishi splits (ESPHome) and their `_proxy` entities report only `heating` / `idle` / `off` and have no
  defrost attribute or entity, so today nothing is shown; there is **no inference** from temperatures. It
  lights up on its own if the firmware starts reporting it.
  (0.4.1) If one spell lasts more than 30 minutes the app stops waiting, goes back to normal logic and records a
  `defrost_stuck` event.
  (0.5.1) Option `defrost_aware` (default off = exactly the behaviour above). On: each defrost window is
  capped at 15 min and at most 4 may start per 60 min, after which normal logic resumes (`defrost_stuck` past the
  cap); the official `defrosting` / `preheating` is always honoured; and on zones with `heat_pump: true` a
  defrost is inferred when the unit reports `heating` in heat mode, the outdoor temp is known and <= 40°F, and
  the room falls >= 0.5°F per 3 min. Unknown outdoor temp = no inference. Debug logs only; zone payload adds
  `defrost_inferred`. Sources: https://developers.home-assistant.io/blog/2024/07/07/climate-hvacaction-defrosting/
  and https://community.home-assistant.io/t/helper-script-to-detect-defrost-status-for-heat-pump/789890
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
  notify_service: notify.mobile_app_example_phone  # optional: phone alerts (persistent notification always)
  motion_sensor: binary_sensor.example_bath_motion # optional: display only
  stale_minutes: 75                                # 0.8.2: 15-120; minutes without a report before fail-off
  last_seen_sensor: sensor.example_node_last_seen  # 0.8.2 optional: Z-Wave "last seen" timestamp sensor
  low_warn_f: 62
  low_warn_min: 10
  low_crit_f: 58
  low_crit_repeat_min: 30
```
Safety (fixed in code): 80°F cap (latched until below setpoint), 2 h max run + 20 min cooldown, 8 h / 24 h lockout
(reset with Off then Heat), sensor unavailable or stale (`stale_minutes`, default 75) → off, no further 1°F rise (or setpoint reached) within 45 min of the last checkpoint → off (0.8.3; reaching the setpoint resets the checkpoint once; a frozen reading at/above the setpoint turns the plug plain off (`holding`, no latch), 0.8.4). Latches, cooldown, checkpoint and 24 h run history persist across restarts in `/data/heater.json` (0.8.4; timestamps sanitized on load; the current run start survives restarts so the 2 h max run cannot be reset by a restart loop; corrupt file → off + 20 min cooldown + alert); heater timers run on a monotonic clock, and a wall clock jump (back > 60 s, or > 60 s off from monotonic elapsed) → off + 20 min cooldown + alert (`clock_jump`, 0.8.5), plug on
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
