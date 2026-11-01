# Smart HVAC — staged install

Quiet hours 12–6 AM ET: no install steps in that window. The manual 71°F hold stays in place throughout.

## Stage A — refresh the store (no effect on climate)
1. HA → Settings → Apps (Add-ons) → App Store → ⋮ → Repositories.
   `https://github.com/bcart180/ha-addons-public` is already listed (CallCatch/Trading Desk).
   If "Check for updates" fails, fallback = copy `smart_hvac/` into `/addons/smart_hvac` (local add-on, Trading Desk model) and
   ⋮ → Check for updates → Local add-ons.
2. ⋮ → **Check for updates**. Confirm "Smart HVAC 0.1.0" appears. Do not click Install yet.

## Stage B — install, master OFF + dry run ON (still no effect)
3. Smart HVAC → **Install** (builds locally from Dockerfile; ~1–3 min).
4. **Configuration** tab: verify `enabled: false`, `dry_run: true` (defaults), set presence
   (e.g. `device_tracker.person_a_phone`), weather and entity IDs for the 3 zones. Save only if something differs.
5. Info tab: Start on boot ON, Watchdog ON, Show in sidebar ON. **Start** is allowed only with a separate GO;
   started in this state it only reads states and renders the panel (writes are code-gated off).
6. Verify: Log shows `enabled=False dry_run=True zones=3`; sidebar "Climate" panel loads; banner reads
   "Smart HVAC is off"; mini splits still heat at 71°F (no climate service calls in HA logbook from the add-on).

## Stage C — go-live
7. Panel → tap **Guest** (keeps 71°F while the owner's phone is away).
8. Set `enabled: true` (dry_run still true) → Restart → watch one 5-min cycle: each zone shows the intended target.
9. Set `dry_run: false` → Restart → after one cycle confirm both mini splits read 71°F heat, bath floor as configured.
10. Then re-enable or retire the paused automations (family leaves/arrives) and the old radiant automations.

## Rollback
Set `enabled: false` (instant, no writes) or Stop the add-on; Uninstall removes it. Nothing in /config is changed.
