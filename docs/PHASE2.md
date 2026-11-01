# Trading Desk — Phase 2 plan (animated room)

Already stubbed in v0.1.0: an SVG "Room" panel with one desk per bot (border
colour = bot status) and dashed hand-off lines with a moving packet, driven by
`pipeline[]` events; a 5-minute price chart for the tapped position; a thresholds
panel fed by `caps` + `thresholds[]`.

Phase 2 work:
1. Full-screen room view (`#room` route / toggle): canvas layout like the
   GROKBOT + JEV reference, desks on a floor grid, per-bot avatar sprite with a
   tiny state machine (idle → typing → walking to hand-off) in the pixel-agents
   style; respect `prefers-reduced-motion`; cap to 30 fps; pause when the tab is hidden.
2. Hand-off animation queue: each new `pipeline` event (keyed by trade_id+stage+ts)
   enqueues one animation from the previous stage's desk to this one, with the
   symbol on the packet; fills (`trade_updates` fill events from Alpaca) flash the
   Execution Trader desk and drop the packet on a "book" shelf.
3. Price chart: candles + VWAP for the selected symbol, entry and stop lines
   from the position and its resting stop order, live last-trade tick from the data stream.
4. Thresholds panel: gauges for every numeric threshold the desk publishes
   (per-trade risk, daily loss kill, crypto sleeve) with breach colouring.
5. Echo Show "room only" kiosk layout (`?view=room`) for the bedside/office screens.
6. Optional: publish HA sensors (equity, day P&L, open-risk %) from the add-on
   via the Supervisor API so automations can alert on cap breaches (still read-only to Alpaca).
