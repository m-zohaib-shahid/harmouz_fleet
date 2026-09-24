// tools/ws_fleet_check.mjs — verifies the new on-demand `{type:"fleet"}` WS request
// returns a full snapshot (used by the dashboard when its store is empty).
const ws = new WebSocket('ws://localhost:8000/ws?token=command-alpha');
let snapshots = 0;
const log = [];

const done = (msg) => {
  console.log(msg);
  for (const l of log) console.log(' - ' + l);
  try {
    ws.close();
  } catch {
    /* ignore */
  }
  process.exit(0);
};

const timer = setTimeout(() => done('TIMEOUT: no fleet snapshot received'), 8000);

ws.onopen = () => {
  log.push('socket open');
  setTimeout(() => ws.send(JSON.stringify({ type: 'fleet' })), 600);
};

ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.type === 'snapshot') {
    snapshots += 1;
    log.push(`snapshot #${snapshots}: ships=${m.ships?.length} ports=${m.ports?.length} zones=${m.zones?.length}`);
    if (snapshots >= 2) {
      clearTimeout(timer);
      done(snapshots >= 2 ? 'PASS: {type:"fleet"} returned a fresh snapshot' : 'FAIL');
    }
  } else {
    log.push('frame: ' + m.type);
  }
};

ws.onerror = () => done('ERROR: socket error');
