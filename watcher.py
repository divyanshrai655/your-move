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
  python3 watcher.py --share         # also open a public link so friends can join your rooms

The games you follow are saved in follow.json next to this file, so they survive restarts.
Standard library only. Desktop popups need macOS; elsewhere moves are printed.
"""
import atexit, functools, http.server, json, os, re, shutil, signal, socket, subprocess, sys, threading, time
import urllib.parse, urllib.request

PORT = int(os.environ.get("PORT", 8765))
APP = f"http://localhost:{PORT}/"
API = "https://lichess.org"
HEADERS = {"User-Agent": "your-move-watcher"}
HERE = os.path.dirname(os.path.abspath(__file__))
FOLLOW_FILE = os.environ.get("FOLLOW_FILE", os.path.join(HERE, "follow.json"))
AUTO_OPEN = "--open" in sys.argv
PRINT_ONLY = "--print" in sys.argv  # log moves in the terminal, no popups
SHARE = "--share" in sys.argv  # open a public cloudflared link to this watcher for friends
public_url = None  # that link, once cloudflared has one

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


# ---------- rooms: watch a game with friends ----------
# A room has one game, a chat, and everyone's locked-in moves. Browsers keep a request open to
# /room/<id>/poll and get the room back whenever it changes. You lock in a move before the player
# makes it, and it can't be changed. Others see THAT you locked in, but not what, until you've locked
# in too or the real move has been played. Rooms are saved to rooms.json so links survive restarts.
ROOMS_FILE = os.environ.get("ROOMS_FILE", os.path.join(HERE, "rooms.json"))
ROOM_ID = re.compile(r"[a-z0-9]{6}")
CID = re.compile(r"[A-Za-z0-9]{8,40}")
UCI = re.compile(r"[a-h][1-8][a-h][1-8][qrbn]?")
MAX_CHAT = 300
MAX_ROOMS = 200   # caps so one person can't fill the disk or tie up every thread
MAX_PEOPLE = 30   # people per room
PRESENT = 40      # seconds: someone who hasn't asked for news in this long has left
WAIT = 25         # seconds a request for news waits for something to happen
rooms = {}  # room id -> Room
news = threading.Condition(lock)  # signalled whenever any room changes


def clean_name(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()[:24]


class Room:
    def __init__(self, rid, saved=None):
        s = saved or {}
        self.id = rid
        self.game = s.get("game")            # {"id", "url", "name"} or None
        self.chat = s.get("chat", [])        # [{"id", "name", "text", "t", "ply"}]
        self.picks = s.get("picks", {})      # game id -> ply (str) -> client id -> {"name", "san", "uci"}
        self.played = s.get("played", {})    # game id -> how many moves the game has had
        for i, m in enumerate(self.chat):    # rooms saved before messages had ids
            m.setdefault("id", i + 1)
        self.next_id = max([m["id"] for m in self.chat], default=0) + 1
        self.seen = {}                       # client id -> {"name", "t"}: who has asked for news lately
        # Goes up on every change. It's time-based, so it keeps going up across restarts and a browser
        # that was waiting for "anything after version N" still hears about the next change.
        self.version = int(time.time() * 1000)

    def saved(self):
        return {"game": self.game, "chat": self.chat, "picks": self.picks, "played": self.played}

    def people(self):
        return sorted({x["name"] for x in self.seen.values()})

    def touch(self, cid, name, now):
        """Note that cid is here. Returns True when the list of people changed."""
        old = self.seen.get(cid)
        self.seen[cid] = {"name": name, "t": now}
        return not old or old["name"] != name

    def prune(self, now):
        """Forget people who stopped asking (closed the tab). Returns True when anyone left."""
        gone = [c for c, x in self.seen.items() if now - x["t"] > PRESENT]
        for c in gone:
            del self.seen[c]
        return bool(gone)

    def bump(self):
        with lock:
            self.version = max(self.version + 1, int(time.time() * 1000))
            news.notify_all()

    def view_picks(self, cid):
        """Everyone's locked moves as `cid` may see them: hidden until cid locked that move too, or it was played."""
        out = {}
        for g, plies in self.picks.items():
            for p, by in plies.items():
                open_ = cid in by or self.played.get(g, 0) > int(p)
                out.setdefault(g, {})[p] = [
                    {"name": x["name"], "me": c == cid, **({"san": x["san"], "uci": x["uci"]} if open_ else {})}
                    for c, x in by.items()]
        return out

    def view(self, cid, after=0):
        """The room as cid sees it, with the chat messages after id `after`."""
        return {"version": self.version, "id": self.id, "game": self.game, "picks": self.view_picks(cid),
                "played": self.played, "people": self.people(), "chat": [m for m in self.chat if m["id"] > after]}

    def apply(self, cid, name, body):
        """One change from a client. Returns the kind of event to send everyone, or raises ValueError."""
        kind = body.get("type")
        if kind == "chat":
            text = str(body.get("text", "")).strip()[:500]
            if not text:
                raise ValueError("empty message")
            ply = body.get("ply")
            self.chat.append({"id": self.next_id, "name": name, "text": text, "t": int(time.time() * 1000),
                              "ply": ply if isinstance(ply, int) else None})
            self.next_id += 1
            del self.chat[:-MAX_CHAT]
            return "chat"
        if kind == "lock":
            g, ply, uci, san = str(body.get("game", "")), body.get("ply"), str(body.get("uci", "")), str(body.get("san", ""))[:12]
            if not g or not isinstance(ply, int) or ply < 0 or not UCI.fullmatch(uci) or not san:
                raise ValueError("expected game, ply, uci, san")
            if ply < self.played.get(g, 0):
                raise ValueError("that move has already been played")
            by = self.picks.setdefault(g, {}).setdefault(str(ply), {})
            if cid in by:
                raise ValueError("you already locked in this move")
            by[cid] = {"name": name, "san": san, "uci": uci}
            return "picks"
        if kind == "played":  # any client tells us the game has reached `ply` moves; that reveals the picks before it
            g, ply = str(body.get("game", "")), body.get("ply")
            if not g or not isinstance(ply, int) or ply < 0:
                raise ValueError("expected game, ply")
            if ply <= self.played.get(g, 0):
                return None
            self.played[g] = ply
            return "picks"
        if kind == "game":
            g = body.get("game") or {}
            gid, url = str(g.get("id", "")), str(g.get("url", ""))
            if not gid or not parse(url):
                raise ValueError("expected a Lichess game")
            self.game = {"id": gid, "url": url, "name": str(g.get("name", ""))[:80]}
            return "game"
        raise ValueError("unknown change")


def get_room(rid, create=True):
    with lock:
        if rid not in rooms and create and len(rooms) < MAX_ROOMS:
            rooms[rid] = Room(rid)
        return rooms.get(rid)


def save_rooms():
    try:
        with lock:
            data = {rid: r.saved() for rid, r in rooms.items()}
        tmp = ROOMS_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(data, fh)
        os.replace(tmp, ROOMS_FILE)
    except OSError as e:
        print("could not save rooms.json:", e)


def load_rooms():
    try:
        with open(ROOMS_FILE) as fh:
            for rid, s in json.load(fh).items():
                if ROOM_ID.fullmatch(rid) and isinstance(s, dict):
                    rooms[rid] = Room(rid, s)
    except (OSError, ValueError):
        pass


def new_room_id():
    import secrets
    while True:
        rid = "".join(secrets.choice("abcdefghjkmnpqrstuvwxyz23456789") for _ in range(6))
        if rid not in rooms:
            return rid


# ---------- web server: the app, /follow, and rooms ----------
# The watcher's list of followed games is the one that counts. GET /follow shows it to the page;
# POST /follow changes ONE game: {"add": {"id", "url", "name"}} or {"remove": id}. There is no way to
# replace the whole list, so a second browser (with its own, maybe empty, bells) can't wipe it.
class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=HERE, **kw)

    def log_message(self, *a):
        pass

    def local(self):
        """True for a browser on this Mac. Friends coming in through a tunnel arrive with the tunnel's
        host name, and get the rooms but not your popups list."""
        if any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "Cf-Ray", "X-Forwarded-For", "Forwarded")):
            return False  # came through a tunnel or proxy, whatever Host it claims
        try:
            host = urllib.parse.urlsplit("//" + (self.headers.get("Host") or "")).hostname
        except ValueError:
            return False
        return host in ("localhost", "127.0.0.1", "::1")

    # Only the app itself is served. Everything else in this folder (.git, follow.json, rooms.json with
    # everyone's hidden moves) stays private, even when friends come in through a tunnel.
    STATIC = {"/": "index.html", "/index.html": "index.html", "/lib.js": "lib.js", "/vendor/chess.js": "vendor/chess.js"}

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path in self.STATIC:
            self.path = "/" + self.STATIC[path]
            return super().do_GET()
        if path == "/rooms":
            return self.reply(200, {"rooms": True, "public": public_url})
        m = re.fullmatch(r"/room/([a-z0-9]{6})/poll", path)
        if m:
            return self.room_poll(m.group(1), urllib.parse.parse_qs(query))
        if path != "/follow":
            return self.reply(404, {"error": "not found"})
        if not self.local():
            return self.reply(403, {"error": "only from this computer"})
        self.reply(200, follows)

    def read_json(self):
        # JSON only: a cross-site page can't send this without a CORS preflight we never approve.
        if self.headers.get("Content-Type") != "application/json":
            raise ValueError
        n = int(self.headers.get("Content-Length", 0))
        if n > 20000:
            raise ValueError
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError
        return body

    def do_POST(self):
        if self.path == "/rooms":
            try:
                self.read_json()
            except ValueError:
                return self.reply(400, {"error": "POST JSON"})
            with lock:
                if len(rooms) >= MAX_ROOMS:
                    return self.reply(503, {"error": "too many rooms"})
                rid = new_room_id()
                rooms[rid] = Room(rid)
            save_rooms()
            return self.reply(200, {"id": rid})
        m = re.fullmatch(r"/room/([a-z0-9]{6})", self.path)
        if m:
            return self.room_post(m.group(1))
        if self.path == "/follow" and not self.local():
            return self.reply(403, {"error": "only from this computer"})
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

    def room_post(self, rid):
        try:
            body = self.read_json()
        except ValueError:
            return self.reply(400, {"error": "POST JSON"})
        cid, name = str(body.get("cid", "")), clean_name(body.get("name"))
        if not CID.fullmatch(cid) or not name:
            return self.reply(400, {"error": "expected cid and name"})
        room = get_room(rid)
        if not room:
            return self.reply(503, {"error": "too many rooms"})
        with lock:
            try:
                kind = room.apply(cid, name, body)
            except ValueError as e:
                return self.reply(400, {"error": str(e)})
            here = room.touch(cid, name, time.time())  # first, so posting also counts as being here
            if kind or here:
                room.bump()
        if kind:
            save_rooms()
        self.reply(200, {"ok": True})

    def room_poll(self, rid, q):
        """Long polling: answer with the room as soon as it's newer than `since`, or after WAIT seconds.
        Plain request/response, so it gets through any tunnel or proxy (Cloudflare quick tunnels don't
        pass server-sent events through)."""
        arg = lambda k: (q.get(k) or [""])[0]
        cid, name = arg("cid"), clean_name(arg("name"))
        try:
            since, after = int(arg("since") or 0), int(arg("after") or 0)
        except ValueError:
            return self.reply(400, {"error": "since and after are numbers"})
        if not CID.fullmatch(cid) or not name:
            return self.reply(400, {"error": "expected cid and name"})
        room = get_room(rid)
        if not room:
            return self.reply(503, {"error": "too many rooms"})
        with lock:
            if cid not in room.seen and len(room.seen) >= MAX_PEOPLE:
                return self.reply(503, {"error": "room is full"})
            now = time.time()
            if room.touch(cid, name, now) | room.prune(now):
                room.bump()
            end = now + WAIT
            while room.version <= since and time.time() < end:
                news.wait(end - time.time())
            view = room.view(cid, after)
        self.reply(200, view)

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


# ---------- sharing: a free, temporary public link through Cloudflare ----------
def share():
    """Start a cloudflared quick tunnel to this watcher and remember its https://….trycloudflare.com link.
    Only the app and rooms are reachable through it (see Handler.STATIC and Handler.local)."""
    exe = shutil.which("cloudflared")
    if not exe:
        print("--share needs cloudflared: brew install cloudflared")
        return
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{PORT}"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    atexit.register(p.terminate)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # `kill` too, not only Ctrl-C, closes the link

    def read():  # keep reading: cloudflared logs to stderr all the time, and a full pipe would stall it
        global public_url
        for line in p.stderr:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not public_url:
                public_url = m.group(0)
                print(f"Sharing at {public_url} - open a room and press Copy link to invite friends.")
        print("The public link has stopped.")
    threading.Thread(target=read, daemon=True).start()


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
    load_rooms()
    if SHARE:
        share()
    print(f"Open {APP} and press the bell on any game to get desktop popups for it.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
