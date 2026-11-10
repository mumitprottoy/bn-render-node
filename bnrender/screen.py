"""The full-screen page on the node's monitor, served by the runner on http://127.0.0.1:8765/ (this PC only).

`/` is the page (everything inline, works offline); `/status.json` is the live state, built from the same data as
`bn status`. The page asks for it every 5 seconds and only changes what changed, so nothing flickers.
"""

import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from .control import read_pause
from .db import Database
from .settings import Settings
from .status import clock
from .status import format_duration
from .status import job_progress

logger = logging.getLogger(__name__)

BIND_ATTEMPTS = 10

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BowlersNetwork render node</title>
<style>
:root {
  --background: #0b0d0a;
  --surface: #12160f;
  --track: #1a2016;
  --accent: #8bc342;
  --text: #e8ece3;
  --muted: #9ba393;
  --line: rgba(232, 236, 227, 0.08);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; background: var(--background); }
body {
  color: var(--text);
  font-family: "Segoe UI", system-ui, sans-serif;
  -webkit-font-smoothing: antialiased;
  cursor: none;
  user-select: none;
  overflow: hidden;
}
main {
  min-height: 100vh;
  display: grid;
  grid-template-rows: auto 1fr auto auto;
  gap: 4vh;
  padding: 7vh 7vw 4vh;
}
h1 {
  font-size: clamp(28px, 5vw, 120px);
  font-weight: 600;
  line-height: 1.08;
  letter-spacing: -0.015em;
  max-width: 18ch;
}
.ask {
  margin-top: 2.2vh;
  font-size: clamp(18px, 2.4vw, 56px);
  font-weight: 400;
  color: var(--accent);
}
.work { align-self: center; }
.headline-row {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 3vw;
  margin-bottom: 2.4vh;
}
.state {
  display: inline-flex;
  align-items: center;
  gap: 0.7em;
  font-size: clamp(15px, 1.5vw, 34px);
  color: var(--text);
  padding: 0.45em 0.95em;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--surface);
}
.state .dot {
  width: 0.62em;
  height: 0.62em;
  border-radius: 50%;
  background: var(--accent);
}
.state[data-state="paused"] .dot,
.state[data-state="waiting"] .dot,
.state[data-state="offline"] .dot { background: var(--muted); }
.state[data-state="restarting"] .dot { background: var(--text); }
.percent {
  font-size: clamp(44px, 7vw, 170px);
  font-weight: 600;
  line-height: 0.9;
  letter-spacing: -0.03em;
  font-variant-numeric: tabular-nums;
}
.percent .sign { color: var(--muted); font-weight: 400; margin-left: 0.06em; }
.bar {
  position: relative;
  height: clamp(14px, 2.4vh, 30px);
  border-radius: 999px;
  background: var(--track);
  box-shadow: inset 0 0 0 1px var(--line);
}
.fill {
  position: absolute;
  inset: 0 auto 0 0;
  width: 0%;
  min-width: clamp(14px, 2.4vh, 30px);
  border-radius: 999px;
  background: var(--accent);
  transition: width 1.6s cubic-bezier(0.22, 0.61, 0.36, 1);
}
.ball {
  position: absolute;
  right: calc(clamp(30px, 5vh, 64px) / -2);
  top: 50%;
  width: clamp(30px, 5vh, 64px);
  height: clamp(30px, 5vh, 64px);
  margin-top: calc(clamp(30px, 5vh, 64px) / -2);
  border-radius: 50%;
  background: radial-gradient(circle at 34% 30%, #3a4433 0%, #1b2117 46%, #0d100b 100%);
  box-shadow: 0 0 0 2px var(--accent), 0 6px 18px rgba(0, 0, 0, 0.45);
  animation: roll 14s linear infinite;
}
.ball::before, .ball::after {
  content: "";
  position: absolute;
  border-radius: 50%;
  background: #050604;
}
.ball::before { width: 15%; height: 15%; left: 30%; top: 24%; box-shadow: 0.42em 0 0 0 #050604; font-size: clamp(30px, 5vh, 64px); }
.ball::after { width: 18%; height: 18%; left: 40%; top: 52%; }
@keyframes roll { to { transform: rotate(360deg); } }
.facts {
  display: grid;
  grid-template-columns: 1.4fr 1fr 1fr;
  gap: 4vw;
  margin-top: 5.5vh;
  padding-top: 3.5vh;
  border-top: 1px solid var(--line);
}
.label {
  font-size: clamp(12px, 1.05vw, 24px);
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 1vh;
}
.value {
  font-size: clamp(18px, 2.1vw, 50px);
  font-weight: 500;
  line-height: 1.2;
  overflow-wrap: anywhere;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.sub {
  margin-top: 0.7vh;
  font-size: clamp(14px, 1.4vw, 32px);
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.thanks {
  font-size: clamp(15px, 1.6vw, 38px);
  color: var(--muted);
}
footer {
  display: flex;
  justify-content: space-between;
  gap: 2vw;
  font-size: clamp(11px, 0.95vw, 22px);
  color: var(--muted);
}
footer .accent { color: var(--accent); }
footer .help { color: var(--muted); opacity: 0.8; }
@media (max-width: 760px) {
  .facts { grid-template-columns: 1fr; gap: 3vh; }
  .headline-row { flex-direction: column; align-items: flex-start; }
  footer { flex-direction: column; }
}
@media (prefers-reduced-motion: reduce) {
  .ball { animation: none; }
  .fill { transition: none; }
}
</style>
</head>
<body>
<main>
  <header>
    <h1>This PC is busy with a very important task.</h1>
    <p class="ask">Please ask Mumit Boss before using it.</p>
  </header>
  <section class="work" aria-live="polite">
    <div class="headline-row">
      <div class="state" id="state-pill" data-state="waiting"><span class="dot"></span><span id="state">Getting ready</span></div>
      <div class="percent"><span id="percent">0</span><span class="sign">%</span></div>
    </div>
    <div class="bar" role="progressbar" aria-label="Overall progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0" id="bar">
      <div class="fill" id="fill"><span class="ball" aria-hidden="true"></span></div>
    </div>
    <div class="facts">
      <div>
        <div class="label">Now making</div>
        <div class="value" id="now-title">Nothing at the moment</div>
        <div class="sub" id="now-detail">&nbsp;</div>
      </div>
      <div>
        <div class="label">Time left</div>
        <div class="value" id="left">Working it out</div>
        <div class="sub" id="around">&nbsp;</div>
      </div>
      <div>
        <div class="label">Finished</div>
        <div class="value" id="today">0 today</div>
        <div class="sub" id="total">0 in total</div>
      </div>
    </div>
  </section>
  <p class="thanks">Thank you for lending this PC to BowlersNetwork.</p>
  <footer>
    <span>Bowlers<span class="accent">Network</span> render node</span>
    <span class="help">Mumit Boss: Win+R, powershell, bn status</span>
    <span id="updated"></span>
  </footer>
</main>
<script>
const PAGE_VERSION = "__PAGE_VERSION__";
let failures = 0;
let restartDeadline = null;

function setText(id, text) {
  const element = document.getElementById(id);
  const value = text === null || text === undefined || text === "" ? "\\u00a0" : String(text);
  if (element.textContent !== value) element.textContent = value;
}

function setState(state, text) {
  const pill = document.getElementById("state-pill");
  if (pill.dataset.state !== state) pill.dataset.state = state;
  setText("state", text);
}

function render(data) {
  const percent = Math.max(0, Math.min(100, Number(data.percent) || 0));
  const shown = percent >= 10 ? percent.toFixed(0) : percent.toFixed(1);
  setText("percent", shown);
  const fill = document.getElementById("fill");
  const width = percent.toFixed(2) + "%";
  if (fill.style.width !== width) fill.style.width = width;
  document.getElementById("bar").setAttribute("aria-valuenow", percent.toFixed(0));
  restartDeadline = data.restart_in === null || data.restart_in === undefined ? null : Date.now() + data.restart_in * 1000;
  setState(data.state, restartText() || data.state_text);
  if (data.current) {
    setText("now-title", data.current.title);
    setText("now-detail", data.current.detail);
  } else {
    setText("now-title", data.state === "finished" ? "Everything is done" : "Nothing at the moment");
    setText("now-detail", "");
  }
  setText("left", data.time_left || (data.state === "finished" ? "Nothing left" : (data.state === "working" ? "Working it out" : "Not known yet")));
  setText("around", data.done_around ? "done around " + data.done_around : "");
  setText("today", data.finished_today.toLocaleString("en-US") + " today");
  setText("total", data.finished_total.toLocaleString("en-US") + " in total");
  setText("updated", "Updated " + data.updated);
}

function restartText() {
  if (restartDeadline === null) return null;
  const seconds = Math.max(0, Math.round((restartDeadline - Date.now()) / 1000));
  return "Restarting in " + seconds + " s";
}

async function refresh() {
  try {
    const response = await fetch("/status.json", { cache: "no-store" });
    if (!response.ok) throw new Error("HTTP " + response.status);
    const data = await response.json();
    failures = 0;
    if (data.page_version && data.page_version !== PAGE_VERSION) {
      window.location.reload();
      return;
    }
    render(data);
  } catch (error) {
    failures += 1;
    if (failures >= 2) setState("offline", "Reconnecting");
  }
}

setInterval(function () {
  const text = restartText();
  if (text) setState("restarting", text);
}, 1000);
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""

PAGE_VERSION = hashlib.sha256(PAGE.encode('utf-8')).hexdigest()[:12]
PAGE_BYTES = PAGE.replace('__PAGE_VERSION__', PAGE_VERSION).encode('utf-8')


def _start_of_today(now: float) -> float:
    local = time.localtime(now)
    return time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))


def _current_item(db: Database, paused: bool) -> dict | None:
    rows = db.query(
        "SELECT items.*, jobs.device AS job_device FROM items JOIN jobs ON jobs.id = items.job_id "
        "WHERE jobs.removed = 0 AND items.active_phase != '' "
        "ORDER BY (items.active_phase = 'render') DESC, (jobs.device = 'gpu') DESC, items.updated_at DESC LIMIT 1")
    if not rows and paused:
        rows = db.query("SELECT * FROM items WHERE state = 'running' AND frames_done > 0 ORDER BY updated_at DESC LIMIT 1")
    if not rows:
        return None
    row = rows[0]
    try:
        title = str(json.loads(row['data']).get('title') or row['asset_id'])
    except ValueError:
        title = row['asset_id']
    phase = row['active_phase']
    total = row['frames_total']
    if phase == 'finish':
        detail = 'checking it and making the video'
    elif phase == 'upload':
        detail = 'sending it to the library'
    elif total:
        frame = min(row['frames_done'] + (1 if phase == 'render' else 0), total)
        detail = f'frame {frame} of {total}' if phase == 'render' else f'stopped at frame {row["frames_done"]} of {total}'
    else:
        detail = 'starting' if phase == 'render' else str()
    return dict(title=title, detail=detail)


def _time_left(seconds: float) -> str:
    text = format_duration(seconds)
    return text if text.startswith('under') else f'about {text}'


def screen_state(settings: Settings, db: Database, info: dict, now: float | None = None) -> dict:
    """What the screen shows. `info` comes from the running worker: restart_in, finished, gpu_problem."""
    now = time.time() if now is None else now
    pause = read_pause(settings)
    overview = job_progress(db, settings, paused=pause is not None)
    active = db.one("SELECT 1 FROM items WHERE active_phase != '' LIMIT 1") is not None
    finished = bool(info.get('finished')) or db.get_meta('finished_at') is not None
    restart_in = info.get('restart_in')
    if finished:
        state, text = 'finished', 'Finished'
    elif restart_in is not None:
        state, text = 'restarting', f'Restarting in {int(restart_in)} s'
    elif pause is not None:
        state, text = 'paused', 'Paused by Mumit Boss'
    elif active:
        state, text = 'working', 'Working'
    elif info.get('gpu_problem'):
        state, text = 'waiting', 'Waiting for the graphics card'
    else:
        state, text = 'waiting', 'Waiting for new work'
    eta = overview.eta_seconds
    # a time is only shown while work is under way: a waiting node has no honest estimate
    show_time = state in ('working', 'restarting') and eta > 0
    today = _start_of_today(now)
    confirmed = db.one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(confirmed_at >= ?), 0) AS today FROM uploads "
        "WHERE state = 'confirmed' AND asset_id NOT LIKE 'catalogue/%'", (today,))
    return dict(
        state=state,
        state_text=text,
        percent=round(overview.percent, 2) if not finished else 100.0,
        units_done=overview.units_done,
        units_total=overview.units_total,
        time_left=_time_left(eta) if show_time else str(),
        done_around=clock(now + eta + 30, with_day=True) if show_time else str(),
        current=None if finished else _current_item(db, paused=pause is not None),
        finished_today=int(confirmed['today'] or 0),
        finished_total=int(confirmed['total'] or 0),
        restart_in=None if restart_in is None else int(restart_in),
        updated=time.strftime('%H:%M:%S', time.localtime(now)),
        page_version=PAGE_VERSION,
    )


class ScreenServer:
    """Serves the page and /status.json on 127.0.0.1 only, from a background thread."""

    def __init__(self, settings: Settings, info: Callable[[], dict]) -> None:
        self.settings = settings
        self.info = info
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        return None if self.server is None else self.server.server_address[1]

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}/'

    def start(self) -> bool:
        handler = self._handler()
        for attempt in range(BIND_ATTEMPTS):
            try:
                self.server = ThreadingHTTPServer(('127.0.0.1', self.settings.screen_port), handler)
                break
            except OSError as error:
                # The previous runner may still be letting go of the port after a restart.
                if attempt == BIND_ATTEMPTS - 1:
                    logger.warning(f'The screen page could not use port {self.settings.screen_port}: {error}')
                    return False
                time.sleep(1)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name='screen', daemon=True)
        self.thread.start()
        logger.info(f'Screen page at {self.url}')
        return True

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def _handler(self) -> type:
        screen = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *arguments: object) -> None:
                return

            def _send(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split('?', 1)[0]
                if path == '/':
                    self._send(200, PAGE_BYTES, 'text/html; charset=utf-8')
                    return
                if path == '/status.json':
                    db = Database(screen.settings.db_path)
                    try:
                        db.init_schema()
                        payload = screen_state(screen.settings, db, screen.info())
                    except Exception as error:
                        logger.exception('The screen state could not be read')
                        self._send(503, json.dumps(dict(error=str(error))).encode('utf-8'), 'application/json')
                        return
                    finally:
                        db.close()
                    self._send(200, json.dumps(payload).encode('utf-8'), 'application/json')
                    return
                self._send(404, b'Not found', 'text/plain; charset=utf-8')

        return Handler
