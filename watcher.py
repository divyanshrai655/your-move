#!/usr/bin/env python3
"""Your Move desktop watcher.

Serves the web app on http://localhost:8765. Turn the bell on for any game in the app,
and from then on every new move in that game pops up a desktop dialog
("23... Be2 - what would you play?"), even with the browser closed. "Open board"
opens the app on that game.

  python3 watcher.py                 # follow whatever you belled in the app
  python3 watcher.py <url> [...]     # also follow these Lichess game/broadcast URLs
  python3 watcher.py --open          # open the board straight away instead of asking
  python3 watcher.py --print         # just log moves in the terminal (PORT=... to change port)

The games you follow are saved in follow.json next to this file, so they survive restarts.
Standard library only. Desktop popups need macOS; elsewhere moves are printed.
"""
import functools, http.server, json, os, re, socket, subprocess, sys, threading, time
import urllib.parse, urllib.request

PORT = int(os.environ.get("PORT", 8765))
APP = f"http://localhost:{PORT}/"
API = "https://lichess.org"
HEADERS = {"User-Agent": "your-move-watcher"}
HERE = os.path.dirname(os.path.abspath(__file__))
FOLLOW_FILE = os.environ.get("FOLLOW_FILE", os.path.join(HERE, "follow.json"))
AUTO_OPEN = "--open" in sys.argv
PRINT_ONLY = "--print" in sys.argv  # log moves in the terminal, no popups

follows = {}   # game id -> {"url", "name"}; "<roundId>:*" follows every game in a round
running = {}   # source (round or game) id -> threading.Event that stops its thread
known = {}     # game id -> plies already seen (no popup for those)
dialogs = set()
lock = threading.RLock()


# ---------- which games to follow ----------
def parse(url):
    """-> (kind, source id, game id or None), or None if it's not a Lichess game/broadcast URL."""
    p = [x for x in urllib.parse.urlparse(url).path.split("/") if x]
    if "broadcast" in p:
        b = p.index("broadcast")
        if len(p) > b + 3:
            return "round", p[b + 3], (p[b + 4] if len(p) > b + 4 else None)
    elif p and re.fullmatch(r"[A-Za-z0-9]{8,12}", p[0]):
        return "game", p[0][:8], p[0][:8]
    return None


def wanted(gid, sid):
    return gid in follows or f"{sid}:*" in follows


def set_follows(new):
    """Replace the followed games, starting/stopping one Lichess stream per round or game."""
    with lock:
        follows.clear()
        follows.update(new)
        need = {}
        for f in follows.values():
            got = parse(f["url"])
            if got:
                need[got[1]] = got[0]
        for sid in [s for s in running if s not in need]:
            running.pop(sid).set()
        for sid, kind in need.items():
            if sid not in running:
                stop = running[sid] = threading.Event()
                target = watch_round if kind == "round" else watch_game
                threading.Thread(target=target, args=(sid, stop), daemon=True).start()
        try:
            with open(FOLLOW_FILE, "w") as fh:
                json.dump(follows, fh, indent=1)
        except OSError as e:
            print("could not save follow.json:", e)
    names = [f.get("name") or k for k, f in follows.items()]
    print("Following:", ", ".join(names) if names else "nothing yet - turn on a bell in the app")


# ---------- web server: the app, plus /follow ----------
# The watcher's list of followed games is the one that counts. GET /follow shows it to the page;
# POST /follow changes ONE game: {"add": {"id", "url", "name"}} or {"remove": id}. There is no way to
# replace the whole list, so a second browser (with its own, maybe empty, bells) can't wipe it.
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path != "/follow":
            return super().do_GET()
        self.reply(200, follows)

    def do_POST(self):
        # JSON only: a cross-site page can't send this without a CORS preflight we never approve.
        if self.path != "/follow" or self.headers.get("Content-Type") != "application/json":
            return self.reply(400, {"error": "POST JSON to /follow"})
        usage = {"error": 'expected {"add": {"id", "url", "name"}} or {"remove": id}'}
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        except ValueError:
            return self.reply(400, usage)
        if not isinstance(body, dict):
            return self.reply(400, usage)
        with lock:
            new = dict(follows)
        if isinstance(body.get("add"), dict):
            g = body["add"]
            gid, url = str(g.get("id", "")), str(g.get("url", ""))
            if not gid or not parse(url):
                return self.reply(400, usage)
            new[gid] = {"url": url, "name": str(g.get("name", ""))}
        elif "remove" in body:
            new.pop(str(body["remove"]), None)
        else:
            return self.reply(400, usage)
        set_follows(new)
        self.reply(200, follows)

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ---------- popups ----------
def on_moves(gid, sid, name, sans, link, over, silent):
    n = len(sans)
    with lock:
        seen = known.get(gid)
        known[gid] = max(n, seen or 0)
    if silent or seen is None or n <= seen or not wanted(gid, sid):
        return
    p = n - 1
    label = f"{p // 2 + 1}{'.' if p % 2 == 0 else '...'} {sans[-1]}"
    side = "White" if n % 2 == 0 else "Black"
    print(time.strftime("%H:%M:%S"), name, label)
    text = f"{name}\n{label}\n\n" + ("Game over." if over else f"{side} to move. What would you play?")
    popup(gid, text, APP + "#" + urllib.parse.quote(link, safe=""))


def popup(gid, text, link):
    if AUTO_OPEN:
        return subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", link])
    if PRINT_ONLY or sys.platform != "darwin":
        return
    with lock:
        if gid in dialogs:  # one dialog per game; the board always shows the latest move anyway
            return
        dialogs.add(gid)

    def run():
        q = lambda s: '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
        script = (f"display dialog {q(text)} with title \"Your Move\" buttons {{\"Later\", \"Open board\"}} "
                  f"default button \"Open board\" giving up after 120")
        try:
            out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout
            if "Open board" in out:
                subprocess.run(["open", link])
        finally:
            with lock:
                dialogs.discard(gid)
    threading.Thread(target=run, daemon=True).start()


# ---------- Lichess streams ----------
def lines(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=900) as r:  # long thinks are quiet; reconnect after 15 min
        for raw in r:
            yield raw.decode("utf-8", "replace")


def pgn_games(text):
    for chunk in text.split("\n\n\n"):
        if "[" not in chunk:
            continue
        h = dict(re.findall(r'^\[(\w+) "(.*)"\]\s*$', chunk, re.M))
        body = chunk.split("\n\n", 1)[1] if "\n\n" in chunk else ""
        body = re.sub(r"\{[^}]*\}|\([^)]*\)", " ", body)
        yield h, [t for t in body.split() if not re.fullmatch(r"\d+\.+|\*|1-0|0-1|1/2-1/2|\$\d+", t)]


def watch_round(rid, stop):
    done = {}

    def handle(chunk, silent):
        for h, sans in pgn_games(chunk):
            gid = h.get("GameURL", "").rstrip("/").split("/")[-1] or f"{h.get('White')}-{h.get('Black')}"
            done[gid] = h.get("Result", "*") != "*"
            link = h.get("GameURL") or f"{API}/broadcast/-/-/{rid}/{gid}"
            on_moves(gid, rid, f"{h.get('White')} – {h.get('Black')}", sans, link, done[gid], silent)

    try:  # snapshot first: marks existing moves as seen, and the stream is silent for finished rounds
        handle("".join(lines(f"{API}/api/broadcast/round/{rid}.pgn")), silent=True)
    except Exception as e:
        print(f"Round {rid}: snapshot failed: {e}")
    delay = 3
    while not stop.is_set():
        mine = [g for g in done if wanted(g, rid)]
        if mine and all(done[g] for g in mine):
            print(f"Round {rid}: the games you follow are over.")
            break
        got, buf = False, ""
        try:
            for line in lines(f"{API}/api/stream/broadcast/round/{rid}.pgn"):
                if stop.is_set():
                    return
                got, buf = True, buf + line
                if buf.endswith("\n\n\n"):
                    handle(buf, silent=False)
                    buf = ""
            if buf.strip():
                handle(buf, silent=False)
            delay = 3 if got else min(delay * 2, 60)  # silent stream = round not started yet or over
        except Exception as e:
            print(f"Round {rid}: {e}; reconnecting")
            delay = 61 if "429" in str(e) else min(delay * 2, 60)
        stop.wait(delay)
    with lock:
        if running.get(rid) is stop:
            running.pop(rid)


def watch_game(gid, stop):
    delay, name = 3, gid
    while not stop.is_set():
        sans, over, quiet_until = [], False, time.time() + 3  # history replayed on connect isn't new
        try:
            for line in lines(f"{API}/api/stream/game/{gid}"):
                if stop.is_set():
                    return
                if not line.strip():
                    continue
                o = json.loads(line)
                if "id" in o:  # game info: sent first, and again at the end with status/winner
                    pl = lambda c: (o.get("players", {}).get(c, {}).get("user") or {}).get("name", c)
                    name = f"{pl('white')} – {pl('black')}"
                    st = (o.get("status") or {}).get("name")
                    over = bool(st) and st not in ("created", "started")
                elif o.get("lm"):
                    sans.append(o["lm"])
                    on_moves(gid, gid, name, sans, f"{API}/{gid}", False, silent=time.time() < quiet_until)
            if over:
                print(f"{name}: game over.")
                break
            delay = 3
        except Exception as e:
            print(f"{name}: {e}; reconnecting")
            delay = 61 if "429" in str(e) else min(delay * 2, 60)
        stop.wait(delay)
    with lock:
        if running.get(gid) is stop:
            running.pop(gid)


def port_in_use(port):
    """'localhost' can mean 127.0.0.1 or ::1, and another server may hold either one,
    so check both before starting. Returns the busy address, or None."""
    for family, host in ((socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")):
        try:
            with socket.socket(family, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                if s.connect_ex((host, port)) == 0:
                    return host
        except OSError:  # e.g. IPv6 disabled
            pass
    return None


def main():
    try:
        with open(FOLLOW_FILE) as fh:  # CLI follows from a previous run don't come back unless named again
            saved = {k: v for k, v in json.load(fh).items() if not v.get("cli")}
    except (OSError, ValueError, AttributeError):
        saved = {}
    for u in [a for a in sys.argv[1:] if not a.startswith("--")]:
        got = parse(u)
        if not got:
            sys.exit(f"Not a Lichess broadcast or game URL: {u}")
        kind, sid, gid = got
        saved[gid or f"{sid}:*"] = {"url": u, "name": "", "cli": True}
    busy = port_in_use(PORT)
    if busy:
        sys.exit(f"Port {PORT} is already in use on {busy}, so the browser might open that instead of this app.\n"
                 f"Stop it first (e.g. a 'python3 -m http.server' or another watcher), then retry.\n"
                 f"To see what it is: lsof -nP -iTCP:{PORT} -sTCP:LISTEN")
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        sys.exit(f"Can't use port {PORT}: {e}")
    set_follows(saved)
    print(f"Open {APP} and press the bell on any game to get desktop popups for it.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
