# Tesla Smart Charge

Charges your Tesla at home with the **lowest current that still reaches your target by departure**, only in
off-peak hours. Gentler on the wall circuit and battery than charging flat out, and it finishes just before you
leave instead of letting the pack sit full.

## Safe by default
Installs with `dry_run: true`. In dry run it plans every check and logs what it would do, but it **never turns
charging on, changes amps, or changes the limit**. Safety stops (peak with no charge-now, unknown sensors) still
fire, so dry run can only make charging stop, never start. Note: this means dry run **can stop a charge you
started by hand during peak** (set `dry_run_safety_off: false` to make dry run send nothing at all). Set `dry_run: false` to go live.

Disable any existing smart-charge automations for the same car before going live, so two controllers don't fight.

## How it works
- Every `check_interval_min` (default 10) it reads battery %, target, departure and voltage, and computes
  `amps = ceil(kWh_needed / efficiency * 1000 / (volts * off-peak hours left))`, clamped to `min_amps..max_amps`.
  Hours left end `finish_buffer_min` (default 30) before departure, which covers the slow taper near the top.
  If a `peak_schedule` is given, only off-peak hours before the finish time count.
- It only sends a new current when it differs by at least 1 A.
- It re-plans **immediately** on the peak → off-peak edge (no fixed-clock race), on helper toggles and on plug
  changes.
- Commands go to the BLE entity first (if set), wait up to `confirm_s` (default 20 s) for the state to change, then
  fall back to the cloud entity. Wake is pressed first when an `online_sensor` reports the car asleep.
- Peak fail-safe: it never charges during peak unless the **charge now** helper is on. An unknown peak sensor counts
  as peak. Unknown battery or plug state never starts a charge.
- If the car is plugged in and should be charging but isn't `notify_not_charging_min` (default 20) minutes after the
  off-peak window opens, it sends one notification per window to `notify_service`.
- A status sensor (`sensor.tesla_smart_charge_status` by default) shows the last decision, reason, amps and target.

## Phone / dashboard controls
Create these helpers (Settings → Devices & services → Helpers → Toggle) and point the options at them. Each one can
go on a dashboard, in an iOS/Android widget, or in a Shortcut that calls `input_boolean.turn_on`.
- `charge_now_entity`: charge now at max amps, even in peak. Turns itself off at target or when unplugged.
- `pause_entity`: stop now and **stay stopped** until the next off-peak window; it clears itself at that edge.
- `trip_entity`: use `trip_target_soc` and `trip_departure_time` instead of the normal target/departure. Turns itself
  off when the car is unplugged.
- `enabled_entity` (optional): master on/off.

## Quick start: cloud-only (no Bluetooth hardware)
Works with any Tesla integration that gives a charge switch, charge-current number, battery level and plug sensor
(Tesla Fleet, Teslemetry, Tessie):
```yaml
dry_run: true
charge_switch: switch.my_car_charge
charge_current: number.my_car_charge_current
charge_limit: number.my_car_charge_limit
battery_level: sensor.my_car_battery_level
plugged_in: binary_sensor.my_car_charge_cable
online_sensor: binary_sensor.my_car_status
wake_button: button.my_car_wake
battery_kwh: 75
target_soc: 80
departure_time: "07:00"
max_amps: 32
peak_schedule:
  - {days: mon-fri, start: "17:00", end: "21:00"}
```
Watch the add-on log: the startup self-check lists every entity it found or could not find.

## BLE + cloud
Add an ESPHome Tesla BLE board's entities; each command goes over Bluetooth first and falls back to the cloud if
the car doesn't confirm within `confirm_s`:
```yaml
ble_charge_switch: switch.tesla_ble_charger
ble_charge_current: number.tesla_ble_charging_amps
ble_wake_button: button.tesla_ble_wake
```

## Dashboard card
1. Copy `tesla_smart_charge/lovelace/tesla-smart-charge-card.js` to `/config/www/tesla-smart-charge-card.js`.
2. Settings → Dashboards → ⋮ → Resources → Add `/local/tesla-smart-charge-card.js` (JavaScript module).
3. Add a card: `type: custom:tesla-smart-charge-card` (optional `entity:` if you changed `status_entity`, `title:`).

The card reads only the status sensor. The chips call `input_boolean.turn_on/turn_off` on the helpers you set in
`charge_now_entity` / `pause_entity` / `trip_entity` (a chip is greyed out if its helper isn't set); Smart turns
them all off. The add-on still applies every rule (peak fail-safe, dry run, caps).

## Taper and cost
- `taper_soc` (default 90) / `taper_factor` (default 1.5): above this SoC the planner allows 1.5x the time per kWh,
  because the car accepts less than the commanded current near full.
- `rates` (optional) prices each hour for the estimate; `default_price` covers hours no block matches:
```yaml
rates:
  - {days: all, start: "21:00", end: "07:00", price: 0.11}
  - {days: all, start: "07:00", end: "21:00", price: 0.24}
currency: "$"
```

## Example options (placeholder entity ids; use your own)
```yaml
dry_run: true
check_interval_min: 10
charge_switch: switch.my_car_charge
charge_current: number.my_car_charge_current
charge_limit: number.my_car_charge_limit
battery_level: sensor.my_car_battery_level
plugged_in: binary_sensor.my_car_charge_cable
charging_sensor: binary_sensor.my_car_charging
online_sensor: binary_sensor.my_car_status
wake_button: button.my_car_wake
home_entity: device_tracker.my_car_location
ble_charge_switch: switch.tesla_ble_charger
ble_charge_current: number.tesla_ble_charging_amps
ble_wake_button: button.tesla_ble_wake
voltage_sensor: sensor.wall_connector_grid_voltage
fallback_voltage: 240
battery_kwh: 75
charge_efficiency: 0.9
target_soc: 80
target_soc_entity: input_number.car_target_soc
departure_time: "07:00"
finish_buffer_min: 30
min_amps: 5
max_amps: 32
peak_sensor: binary_sensor.utility_peak
peak_schedule:
  - days: mon-fri
    start: "07:00"
    end: "11:00"
  - days: mon-fri
    start: "17:00"
    end: "21:00"
charge_now_entity: input_boolean.car_charge_now
pause_entity: input_boolean.car_charge_pause
trip_entity: input_boolean.car_trip_mode
trip_target_soc: 100
trip_departure_time: "05:30"
notify_service: notify.mobile_app_my_phone
notify_not_charging_min: 20
```

## Options reference
- Required: `charge_switch`, `charge_current`, `battery_level`, `plugged_in`. Until these are set the add-on does
  nothing.
- `voltage_sensor`: readings outside 180–260 V (or unknown) fall back to `fallback_voltage`.
- `max_amps_entity`: optional helper to lower the cap at runtime (never above `max_amps`).
- `departure_entity`: optional `input_datetime` that overrides `departure_time`.
- `dry_run_safety_off` (default true): in dry run, still send safety stops.
- `min_amps` / `max_amps`: 1–48 A. 48 A is a hard ceiling (also enforced in the engine). If `min_amps` is above
  `max_amps`, max wins and a warning is logged.
- `charge_limit` / `ble_charge_limit`: one of them is **required to go live** (`dry_run: false`); without it the
  add-on refuses to start charging (stops still work).
- `charge_on_max_per_window` (default 5): if the car doesn't start charging, the on command is retried with backoff
  10, 20, 40, 80, then 120 min, up to this many times per window.
- `stale_minutes` (default 30): battery level or plug state not reported for this long counts as unknown. The add-on
  then never starts or raises charging; peak safety stops still fire. It presses wake to refresh only when the plug
  is freshly known plugged in, `home_entity` (if set) says home, it is off-peak or charge-now is on, and the last
  known SoC is below target. Wakes back off 10, 20, 40, 80, then every 120 min, at most `wake_max_per_window`
  (default 5, 0 = never) per window, and reset only on a real plug off -> on or a new off-peak window (unavailable
  readings never count). Any two wake presses are always at least 10 minutes apart.
- Writes are limited to `switch.turn_on/off`, `number.set_value`, `button.press` and `input_boolean.turn_off` on the
  entity ids you configured, plus your notify service and the status sensor.
