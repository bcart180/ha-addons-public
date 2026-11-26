#!/usr/bin/env python3
"""Generate every consumer artifact from tokens.json (single source of truth).

Outputs (all checked in, never hand-edited):
  bryson-ui-4.css        CSS custom properties only (link from every page; no element styles)
  bryson-ui-4-base.css   optional base element/component layer for NEW pages
  ha-theme.yaml          Home Assistant frontend theme "BRYSON-UI-4 Charcoal"
  tokens.mjs             ES module export for Lovelace custom cards (Homebase, Echo, HVAC)
Run: python3 shared/design/build.py   (add --check to fail if outputs are stale)
"""
import json, sys, pathlib

HERE = pathlib.Path(__file__).resolve().parent
T = json.loads((HERE / "tokens.json").read_text())
C, F, TY, SP, R, CT = T["color"], T["font"], T["type"], T["space"], T["radius"], T["control"]
HDR = "Generated from shared/design/tokens.json v{} by build.py. Do not edit by hand.".format(T["version"])


def flat_vars():
    v = {}
    for k, val in C.items():
        if isinstance(val, dict):
            pre = "--bui-primary-" if k == "primary-scale" else "--bui-cat-"
            for k2, v2 in val.items():
                v[f"{pre}{k2}"] = v2
        else:
            v[f"--bui-{k}"] = val
    v["--bui-font"] = F["family"]
    v["--bui-font-mono"] = F["family-mono"]
    for k in ("weight-regular", "weight-medium", "weight-bold"):
        v[f"--bui-{k}"] = F[k]
    for k, t in TY.items():
        v[f"--bui-{k}-size"] = t["size"]
        v[f"--bui-{k}-line"] = t["line"]
        v[f"--bui-{k}-tracking"] = t["tracking"]
    for k, val in SP.items():
        v[f"--bui-space-{k}"] = val
    for k, val in R.items():
        v[f"--bui-radius-{k}"] = val
    for k, val in CT.items():
        v[f"--bui-{k}"] = val
    for k, val in T["shadow"].items():
        v[f"--bui-shadow-{k}"] = val
    return v


def css():
    """Tokens only: custom properties, no element styles. Safe to link from any page."""
    v = flat_vars()
    body = "\n".join(f"  {k}: {val};" for k, val in v.items())
    return f"""/* {HDR} */
:root, :host {{
  color-scheme: dark;
{body}
}}
"""


def base_css():
    """Optional opt-in base layer (new pages): element + component classes built on the tokens."""
    return f"""/* {HDR} Optional base layer; link after bryson-ui-4.css. */
html, body {{ background: var(--bui-page); color: var(--bui-text); font-family: var(--bui-font);
  font-size: var(--bui-body-size); line-height: var(--bui-body-line); margin: 0; }}
h1 {{ font-size: var(--bui-h1-size); line-height: var(--bui-h1-line); letter-spacing: var(--bui-h1-tracking); font-weight: var(--bui-weight-medium); }}
h2 {{ font-size: var(--bui-h2-size); line-height: var(--bui-h2-line); font-weight: var(--bui-weight-medium); }}
h3 {{ font-size: var(--bui-h3-size); line-height: var(--bui-h3-line); font-weight: var(--bui-weight-medium); }}
h4 {{ font-size: var(--bui-h4-size); line-height: var(--bui-h4-line); font-weight: var(--bui-weight-bold); }}
small, .bui-small {{ font-size: var(--bui-small-size); line-height: var(--bui-small-line); color: var(--bui-text-secondary); }}
a {{ color: var(--bui-accent-text); }}
.bui-card {{ background: var(--bui-surface); border: 1px solid var(--bui-border); border-radius: var(--bui-radius-card);
  box-shadow: var(--bui-shadow-card); padding: var(--bui-space-4); }}
.bui-muted {{ color: var(--bui-text-secondary); }}
.bui-btn {{ min-height: var(--bui-height); min-width: var(--bui-touch-min); padding: 0 var(--bui-space-4); border: 0;
  border-radius: var(--bui-radius-sm); font: var(--bui-weight-medium) var(--bui-body-size)/1 var(--bui-font);
  background: var(--bui-button-secondary-bg); color: var(--bui-button-secondary-fg); cursor: pointer; }}
.bui-btn--primary {{ background: var(--bui-button-primary-bg); color: var(--bui-button-primary-fg); }}
.bui-btn--primary:hover {{ background: var(--bui-accent-hover); }}
.bui-btn--tertiary {{ background: var(--bui-button-tertiary-bg); color: var(--bui-button-tertiary-fg); }}
.bui-btn:disabled {{ color: var(--bui-text-disabled); background: var(--bui-input-disabled); cursor: default; }}
.bui-tabs {{ display: flex; background: var(--bui-tab-bg); border: 1px solid var(--bui-segment-border); border-radius: var(--bui-radius-md); overflow: hidden; }}
.bui-tab {{ flex: 1; min-height: var(--bui-height-sm); color: var(--bui-tab-fg); background: transparent; border: 0; font: inherit; }}
.bui-tab[aria-selected="true"] {{ background: var(--bui-tab-selected-bg); color: var(--bui-tab-selected-fg); box-shadow: inset 0 -2px 0 var(--bui-tab-indicator); }}
input, select, textarea {{ background: var(--bui-input); color: var(--bui-text); border: 1px solid var(--bui-border-strong); border-radius: var(--bui-radius-sm); }}
:focus-visible {{ outline: 2px solid var(--bui-focus-ring); outline-offset: 2px; }}
.bui-danger {{ color: var(--bui-danger); }} .bui-success {{ color: var(--bui-success); }}
"""


def ha_theme():
    a, at = C["accent"], C["accent-text"]
    m = {
        "primary-color": a, "accent-color": a, "dark-primary-color": C["accent-pressed"], "light-primary-color": C["accent-hover"],
        "text-primary-color": C["text-on-accent"],
        "primary-background-color": C["page"], "lovelace-background": C["page"],
        "secondary-background-color": C["surface"], "card-background-color": C["surface"],
        "ha-card-background": C["surface"], "ha-card-border-radius": R["card"], "ha-card-border-width": "1px",
        "ha-card-border-color": C["border"], "ha-card-box-shadow": "none",
        "ha-dialog-surface-background": C["surface"], "mdc-dialog-surface-color": C["surface"],
        "primary-text-color": C["text"], "secondary-text-color": C["text-secondary"],
        "disabled-text-color": C["text-disabled"], "divider-color": C["border"],
        "paper-font-common-base_-_font-family": F["family"], "primary-font-family": F["family"],
        "state-climate-heat-color": C["heat"], "state-climate-cool-color": C["cool"],
        "state-climate-dry-color": C["dry"], "state-climate-fan-only-color": C["dry"],
        "state-climate-auto-color": C["success"], "state-climate-heat-cool-color": C["category"]["purple"],
        "state-climate-off-color": C["off"],
        "success-color": C["success"], "error-color": C["danger"], "warning-color": C["danger"], "info-color": C["info"],
        "app-header-background-color": C["page"], "app-header-text-color": C["text"],
        "app-header-selection-bar-color": a, "app-header-edit-background-color": C["surface-raised"],
        "sidebar-background-color": C["page"], "sidebar-text-color": C["text"],
        "sidebar-selected-text-color": at, "sidebar-selected-icon-color": at, "sidebar-icon-color": C["text-secondary"],
        "switch-checked-color": a, "switch-checked-button-color": a, "switch-checked-track-color": a,
        "switch-unchecked-button-color": C["text-secondary"], "switch-unchecked-track-color": C["border-strong"],
        "slider-color": a, "slider-track-color": C["border-strong"],
        "paper-slider-active-color": a, "paper-slider-knob-color": a,
        "mdc-theme-primary": a, "mdc-theme-secondary": a, "mdc-theme-background": C["page"],
        "mdc-theme-surface": C["surface"], "mdc-theme-on-primary": C["text-on-accent"], "mdc-theme-on-surface": C["text"],
        "ha-view-sections-column-min-width": "280px", "ha-view-sections-column-max-width": "560px",
        "ha-view-sections-column-gap": SP["4"], "ha-view-sections-row-gap": SP["4"],
        # State colors: accent-text (>= 4.5:1 on surface) for active icons; danger for unavailable.
        "state-icon-color": C["text-secondary"], "state-icon-active-color": at,
        "state-active-color": at, "state-light-active-color": at, "state-light-on-color": at,
        "state-binary_sensor-active-color": at, "state-binary_sensor-on-color": at,
        "state-binary_sensor-occupancy-on-color": at, "state-binary_sensor-occupancy-detected-color": at,
        "state-binary_sensor-occupancy-active-color": at,
        "state-group-on-color": at, "state-group-active-color": at, "state-group-home-color": at,
        "state-person-home-color": at, "state-person-active-color": at,
        "state-device_tracker-home-color": at, "state-device_tracker-active-color": at,
        "state-unavailable-color": C["danger"],
        "orange-color": C["danger"], "amber-color": C["danger"], "yellow-color": C["danger"], "deep-orange-color": C["danger"],
        "rgb-orange-color": "255, 93, 115", "rgb-amber-color": "255, 93, 115",
        "rgb-yellow-color": "255, 93, 115", "rgb-deep-orange-color": "255, 93, 115",
        "rgb-primary-color": "62, 106, 225", "rgb-accent-color": "62, 106, 225",
        "input-fill-color": C["input"], "input-disabled-fill-color": C["input-disabled"],
        "input-ink-color": C["text"], "input-label-ink-color": C["text-secondary"],
        "input-disabled-ink-color": C["text-disabled"], "input-dropdown-icon-color": C["text-secondary"],
        "input-idle-line-color": C["border-strong"], "input-hover-line-color": a, "input-disabled-line-color": C["border"],
        "input-outlined-idle-border-color": C["border-strong"], "input-outlined-hover-border-color": a,
        "input-outlined-disabled-border-color": C["border"],
        "codemirror-property": at, "markdown-code-background-color": C["surface-raised"],
        "table-row-background-color": C["surface"], "table-row-alternative-background-color": C["surface-raised"],
        "data-table-background-color": C["surface"],
    }
    # HA 2025+ design-system palette (ha-button, toggle groups, calendar controls) defaults to cyan; pin to our blue.
    for k2, v2 in C["primary-scale"].items():
        m[f"ha-color-primary-{k2}"] = v2
    lines = [f"# {HDR}", "# Charcoal pages, lighter cards, bright text, accent #3E6AE1. No pure black, no orange/amber.",
             f"{T['name']}:"]
    lines += [f"  {k}: {json.dumps(v)}" for k, v in m.items()]
    # Same tokens under modes.dark so HA knows this is a dark theme (dark fills for
    # ha-button / toggle groups / calendar controls). Top level keeps light-mode clients charcoal too.
    lines += ["  modes:", "    dark:"]
    lines += [f"      {k}: {json.dumps(v)}" for k, v in m.items()]
    return "\n".join(lines) + "\n"


def mjs():
    return f"// {HDR}\nexport const BUI = {json.dumps(flat_vars(), indent=2)};\n" \
           "export const BUI_CSS = Object.entries(BUI).map(([k, v]) => `${k}:${v};`).join('');\n" \
           "export const BUI_TIME = { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' };\n"


OUT = {"bryson-ui-4.css": css(), "bryson-ui-4-base.css": base_css(), "ha-theme.yaml": ha_theme(), "tokens.mjs": mjs()}
if __name__ == "__main__":
    check = "--check" in sys.argv
    stale = []
    for name, text in OUT.items():
        p = HERE / name
        if check:
            if not p.exists() or p.read_text() != text:
                stale.append(name)
        else:
            p.write_text(text)
    if stale:
        sys.exit("stale generated files: " + ", ".join(stale))
    print("ok" if check else "wrote " + ", ".join(OUT))
