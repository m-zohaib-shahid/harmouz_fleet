# tools/

Throwaway-but-useful verification harness for the AegisFleet dashboard. Everything
here runs on Node 20+ with **zero npm dependencies** (uses the built-in `fetch` /
`WebSocket` globals) and drives a real headless Chrome/Edge through the Chrome
DevTools Protocol, so assertions are made against live DOM state - no mocks.

## Run the full end-to-end probe

```powershell
# server must already be serving the dashboard
cd "d:\hakethon\New folder"
node tools\e2e_probe.mjs                                   # defaults to msedge
node tools\e2e_probe.mjs "C:\Program Files\Google\Chrome\Application\chrome.exe"
node tools\e2e_probe.mjs "C:\...\msedge.exe" "http://localhost:8001"
```

It boots the dashboard in headless mode and reports 14 steps (see
`e2e_report.txt` for a captured run). The probe leaves the demo state clean: the
zone it draws is deleted again, and any demo zone it creates is removed via REST.

| Step | What it proves |
| --- | --- |
| 1 | WS is live, the top stat strip is populated, 15 ship markers exist |
| 2 | Clicking a fleet row selects the vessel and renders the command tools |
| 3 | "Issue Directive" lists **all 15 ships** and pre-selects the current vessel |
| 4 | Store internals (ships / ports / selection) |
| 5 | Stability: 4 samples every 2.5 s - the store never loses ships/ports |
| 6 | A reroute directive is accepted by `/api/directives` and reaches the server |
| 7 | Rect-zone drawing via real mouse input: 4 vertices, modal preview, zone created |
| 8 | Clicking a zone polygon opens the manage modal with Activate/Delete |
| 9 | Alert-feed click selects a vessel; distress text is parsed (offline NLP) |
| 10 | Fleet filter, alert severity filter and "clear alerts" all work |
| 11 | Timeline scrub relabels LIVE -> `-30m` and renders a history frame |
| 12 | "Demo Strait" creates the scripted zone and it reaches the store/map |
| 13 | No literal `undefined` / `NaN` leaks into any rendered panel |
| 14 | Zero uncaught runtime errors and zero console errors |

## Check the on-demand fleet frame

```powershell
node tools\ws_fleet_check.mjs
```

Confirms the backend answers `{ "type": "fleet" }` with a fresh full snapshot
(`ships=15 ports=10`), which is what the dashboard falls back to when its store
is empty.
