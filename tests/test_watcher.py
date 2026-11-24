"""Tests for watcher.py: python3 -m unittest discover tests (or ./test.sh).

Offline and silent: Lichess connections and the macOS popup are replaced with stand-ins.
"""
import http.client, json, os, socket, subprocess, sys, tempfile, threading, unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import watcher  # noqa: E402

FIXTURES = os.path.join(ROOT, "tests", "fixtures")


class Isolated(unittest.TestCase):
    """Fresh watcher state for each test, no network, no popups, follow.json in a temp dir."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.popups = []
        self.started = []
        patches = [
            mock.patch.object(watcher, "FOLLOW_FILE", os.path.join(self.tmp.name, "follow.json")),
            mock.patch.object(watcher, "popup", lambda gid, text, link: self.popups.append((gid, text, link))),
            mock.patch.object(watcher, "watch_round", lambda sid, stop: self.started.append(("round", sid))),
            mock.patch.object(watcher, "watch_game", lambda sid, stop: self.started.append(("game", sid))),
            mock.patch("builtins.print"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for d in (watcher.follows, watcher.running, watcher.known):
            d.clear()
        self.addCleanup(self.tmp.cleanup)

    def follow(self, *game_ids, rid="5qZNwCTF"):
        watcher.set_follows({g: {"url": f"https://lichess.org/broadcast/-/-/{rid}/{g}", "name": g} for g in game_ids})


class Links(unittest.TestCase):
    def test_broadcast_game_link(self):
        self.assertEqual(
            watcher.parse("https://lichess.org/broadcast/2026-chinese-chess-league-division-a-finals/round-1-game-2/5qZNwCTF/EdbhF3R2"),
            ("round", "5qZNwCTF", "EdbhF3R2"))

    def test_round_link(self):
        self.assertEqual(watcher.parse("https://lichess.org/broadcast/t/r/xLgOJ6cb"), ("round", "xLgOJ6cb", None))

    def test_game_link(self):
        self.assertEqual(watcher.parse("https://lichess.org/1Rs5kLFbWXYZ"), ("game", "1Rs5kLFb", "1Rs5kLFb"))

    def test_junk(self):
        for s in ("https://example.com/x", "https://lichess.org/broadcast/x", "hello"):
            self.assertIsNone(watcher.parse(s), s)


class ReadingPgn(unittest.TestCase):
    def test_round_fixture(self):
        with open(os.path.join(FIXTURES, "round.pgn")) as fh:
            games = list(watcher.pgn_games(fh.read()))
        self.assertEqual([h["GameURL"].split("/")[-1] for h, _ in games], ["SWxMuX4D", "EdbhF3R2", "j9bPfDPt"])
        h, sans = games[0]
        self.assertEqual(h["Result"], "1/2-1/2")
        self.assertEqual(sans[10], "Bd3")  # comments and move numbers are stripped
        self.assertTrue(all(not s.startswith("{") and not s[0].isdigit() for s in sans))


class Popups(Isolated):
    """The heart of the app: a popup on each new move of a game you follow, and nowhere else."""

    def moves(self, gid, n, silent=False):
        watcher.on_moves(gid, "5qZNwCTF", "A – B", ["e4", "e5", "Nf3", "Nc6"][:n], "link", False, silent)

    def test_new_move_in_followed_game_pops_up(self):
        self.follow("G1")
        self.moves("G1", 2)  # first time we see the game: that's history, not a new move
        self.moves("G1", 3)
        self.assertEqual(len(self.popups), 1)
        gid, text, link = self.popups[0]
        self.assertEqual(gid, "G1")
        self.assertIn("2. Nf3", text)
        self.assertIn("Black to move. What would you play?", text)
        self.assertTrue(link.startswith(watcher.APP + "#"))

    def test_no_popup_for_games_you_dont_follow(self):
        self.follow("G1")
        self.moves("OTHER", 2)
        self.moves("OTHER", 3)
        self.assertEqual(self.popups, [])

    def test_no_popup_after_you_unfollow(self):
        self.follow("G1")
        self.moves("G1", 2)
        self.follow()  # bell turned off in the app
        self.moves("G1", 3)
        self.assertEqual(self.popups, [])

    def test_no_popup_for_history_or_repeats(self):
        self.follow("G1")
        self.moves("G1", 3)                # first sighting
        self.moves("G1", 3)                # reconnect replays the same moves
        self.moves("G1", 4, silent=True)   # loading a snapshot
        self.assertEqual(self.popups, [])

    def test_whole_round_from_command_line(self):
        watcher.set_follows({"5qZNwCTF:*": {"url": "https://lichess.org/broadcast/t/r/5qZNwCTF", "name": ""}})
        self.moves("ANY", 1)
        self.moves("ANY", 2)
        self.assertEqual(len(self.popups), 1)


class Following(Isolated):
    def test_one_connection_per_round_however_many_games(self):
        self.follow("G1", "G2", "G3")
        self.assertEqual(self.started, [("round", "5qZNwCTF")])

    def test_follows_are_saved_for_next_time(self):
        self.follow("G1")
        with open(watcher.FOLLOW_FILE) as fh:
            self.assertIn("G1", json.load(fh))

    def test_unfollowing_everything_stops_the_connection(self):
        self.follow("G1")
        stop = watcher.running["5qZNwCTF"]
        self.follow()
        self.assertTrue(stop.is_set())
        self.assertEqual(watcher.running, {})


class FollowEndpoint(Isolated):
    """The app tells the watcher which games are belled by POSTing to /follow."""

    def setUp(self):
        super().setUp()
        self.srv = watcher.http.server.ThreadingHTTPServer(("127.0.0.1", 0), watcher.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def request(self, method, body=None, ctype="application/json"):
        c = http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)
        c.request(method, "/follow", body=body, headers={"Content-Type": ctype} if body is not None else {})
        r = c.getresponse()
        return r.status, json.loads(r.read() or "null")

    def add(self, gid):
        return self.request("POST", json.dumps({"add": {"id": gid, "url": f"https://lichess.org/broadcast/-/-/5qZNwCTF/{gid}", "name": gid}}))

    def test_a_bell_from_the_app_is_followed(self):
        status, body = self.add("G1")
        self.assertEqual(status, 200)
        self.assertIn("G1", body)
        self.assertEqual(self.request("GET")[1], body)

    def test_a_second_browser_cannot_wipe_the_list(self):
        # Browser A bells G1. Browser B, with its own (empty) bells, opens the page and bells G2.
        self.add("G1")
        _, body = self.add("G2")
        self.assertEqual(sorted(body), ["G1", "G2"])
        # The old "replace everything" request is refused and changes nothing.
        status, _ = self.request("POST", "[]")
        self.assertEqual(status, 400)
        self.assertEqual(sorted(self.request("GET")[1]), ["G1", "G2"])

    def test_removing_one_game_keeps_the_rest(self):
        self.add("G1")
        self.add("G2")
        status, body = self.request("POST", json.dumps({"remove": "G1"}))
        self.assertEqual(status, 200)
        self.assertEqual(list(body), ["G2"])

    def test_non_json_posts_are_refused(self):
        # A web page on another site can send text/plain without asking; JSON needs permission it never gets.
        status, _ = self.request("POST", json.dumps({"remove": "G1"}), ctype="text/plain")
        self.assertEqual(status, 400)

    def test_bad_requests_are_refused(self):
        for body in ("{not json", json.dumps({}), json.dumps({"add": {"id": "G1"}}),
                     json.dumps({"add": {"id": "G1", "url": "https://example.com"}}), json.dumps("G1")):
            self.assertEqual(self.request("POST", body)[0], 400, body)
        self.assertEqual(self.request("GET")[1], {})

    def test_command_line_follows_survive_the_app(self):
        watcher.set_follows({"5qZNwCTF:*": {"url": "https://lichess.org/broadcast/t/r/5qZNwCTF", "name": "", "cli": True}})
        _, body = self.add("G1")
        self.assertIn("5qZNwCTF:*", body)


class Port(unittest.TestCase):
    def listen(self, family, host):
        s = socket.socket(family, socket.SOCK_STREAM)
        s.bind((host, 0))
        s.listen()
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_busy_ipv4(self):
        self.assertEqual(watcher.port_in_use(self.listen(socket.AF_INET, "127.0.0.1")), "127.0.0.1")

    def test_busy_ipv6(self):
        # The case that let a plain server and the watcher share port 8765.
        self.assertEqual(watcher.port_in_use(self.listen(socket.AF_INET6, "::1")), "::1")

    def test_free_port(self):
        port = self.listen(socket.AF_INET, "127.0.0.1")
        self.doCleanups()
        self.assertIsNone(watcher.port_in_use(port))

    def test_watcher_refuses_to_start_on_a_busy_port(self):
        port = self.listen(socket.AF_INET6, "::1")
        env = dict(os.environ, PORT=str(port), FOLLOW_FILE=os.path.join(tempfile.gettempdir(), "unused-follow.json"))
        r = subprocess.run([sys.executable, os.path.join(ROOT, "watcher.py"), "--print"],
                           env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 1)
        self.assertIn("already in use on ::1", r.stderr)


if __name__ == "__main__":
    unittest.main()
