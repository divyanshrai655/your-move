#!/usr/bin/env python3
"""Your Move desktop watcher.

Serves the web app on http://localhost:8765 and, for every new move in the games you
name, pops up a desktop dialog ("23... Be2 - what would you play?"). Choosing
"Open board" opens the app in your browser on that game. Works with no browser open.

  python3 watcher.py <lichess broadcast or game URL> [more URLs...] [--open]

A broadcast URL that ends in a game id watches only that game; a round URL watches
every game in the round. --open skips the dialog and opens the board straight away.
Standard library only. Desktop popups need macOS; elsewhere moves are printed.
"""
import functools, http.server, json, os, re, subprocess, sys, threading, time
import urllib.parse, urllib.request

PORT = 8765
APP = f"http://localhost:{PORT}/"
API = "https://lichess.org"
HEADERS = {"User-Agent": "your-move-watcher"}
AUTO_OPEN = "--open" in sys.argv
known = {}          # game id -> number of plies already seen (no popup for those)
dialogs = set()     # games with a popup currently on screen
lock = threading.Lock()


def serve():
    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a): pass
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), functools.partial(Quiet, directory=here))
    except OSError:
        return print(f"Port {PORT} is busy; assuming the app is already served there.")
    print(f"App: {APP}")
    srv.serve_forever()


def parse(url):
    p = [x for x in urllib.parse.urlparse(url).path.split("/") if x]
    if "broadcast" in p:
        b = p.index("broadcast")
        if len(p) > b + 3:
            return "round", p[b + 3], (p[b + 4] if len(p) > b + 4 else None)
    elif p and re.fullmatch(r"[A-Za-z0-9]{8,12}", p[0]):
        return "game", p[0][:8], None
    sys.exit(f"Not a Lichess broadcast round or game URL: {url}")


def lines(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=900) as r:  # long thinks are quiet; reconnect after 15 min
        for raw in r:
            yield raw.decode("utf-8", "replace")


def move_label(sans, n):
    p = n - 1
    return f"{p // 2 + 1}{'.' if p % 2 == 0 else '...'} {sans[-1]}"


def on_moves(gid, name, sans, link, over, silent):
    """sans: all moves so far. Pops a dialog when the count grows past what we've seen."""
    n = len(sans)
    with lock:
        seen = known.get(gid)
        known[gid] = max(n, seen or 0)
    if silent or seen is None or n <= seen:
        return
    side = "White" if n % 2 == 0 else "Black"
    text = f"{name}\n{move_label(sans, n)}\n\n" + ("Game over." if over else f"{side} to move. What would you play?")
    print(time.strftime("%H:%M:%S"), name, move_label(sans, n))
    popup(gid, text, APP + "#" + urllib.parse.quote(link, safe=""))


def popup(gid, text, link):
    if AUTO_OPEN or sys.platform != "darwin":
        if AUTO_OPEN: subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", link])
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


def pgn_games(text):
    for chunk in text.split("\n\n\n"):
        if "[" not in chunk:
            continue
        h = dict(re.findall(r'^\[(\w+) "(.*)"\]\s*$', chunk, re.M))
        body = chunk.split("\n\n", 1)[1] if "\n\n" in chunk else ""
        body = re.sub(r"\{[^}]*\}|\([^)]*\)", " ", body)
        sans = [t for t in body.split() if not re.fullmatch(r"\d+\.+|\*|1-0|0-1|1/2-1/2|\$\d+", t)]
        yield h, sans


def watch_round(rid, only, src_url):
    def handle(chunk, silent):
        for h, sans in pgn_games(chunk):
            gid = h.get("GameURL", "").rstrip("/").split("/")[-1] or f"{h.get('White')}-{h.get('Black')}"
            if only and gid != only:
                continue
            over = h.get("Result", "*") != "*"
            done[gid] = over
            on_moves(gid, f"{h.get('White')} – {h.get('Black')}", sans, h.get("GameURL") or src_url, over, silent)

    done, delay = {}, 3
    try:  # snapshot first: marks existing moves as seen, and the stream is silent for finished rounds
        handle("".join(lines(f"{API}/api/broadcast/round/{rid}.pgn")), silent=True)
    except Exception as e:
        print("snapshot failed:", e)
    while True:
        if done and all(done.values()):
            return print(f"Round {rid}: finished.")
        got, buf = False, ""
        try:
            for line in lines(f"{API}/api/stream/broadcast/round/{rid}.pgn"):
                got, buf = True, buf + line
                if buf.endswith("\n\n\n"):
                    handle(buf, silent=False)
                    buf = ""
            if buf.strip():
                handle(buf, silent=False)
        except Exception as e:
            print(f"Round {rid}: {e}; reconnecting")
            if "429" in str(e): delay = 61
        delay = 3 if got else min(delay * 2, 60)
        time.sleep(delay)


def watch_game(gid, src_url):
    delay, name = 3, gid
    while True:
        sans, over, quiet_until = [], False, time.time() + 3  # replayed history on connect is not new
        try:
            for line in lines(f"{API}/api/stream/game/{gid}"):
                if not line.strip():
                    continue
                o = json.loads(line)
                if "id" in o:  # game info, sent first and again at the end with status/winner
                    pl = lambda c: (o.get("players", {}).get(c, {}).get("user") or {}).get("name", c)
                    name = f"{pl('white')} – {pl('black')}"
                    st = (o.get("status") or {}).get("name")
                    over = bool(st) and st not in ("created", "started")
                elif o.get("lm"):
                    sans.append(o["lm"])
                    on_moves(gid, name, sans, src_url, False, silent=time.time() < quiet_until)
            if over:
                return print(f"{name}: game over.")
            delay = 3
        except Exception as e:
            print(f"{name}: {e}; reconnecting")
            delay = 61 if "429" in str(e) else min(delay * 2, 60)
        time.sleep(delay)


def main():
    urls = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not urls:
        sys.exit(__doc__)
    threading.Thread(target=serve, daemon=True).start()
    for u in urls:
        kind, sid, only = parse(u)
        target = watch_round if kind == "round" else watch_game
        args = (sid, only, u) if kind == "round" else (sid, u)
        threading.Thread(target=target, args=args, daemon=True).start()
        print(f"Watching {kind} {sid}" + (f" (game {only})" if only else ""))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
