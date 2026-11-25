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


class Rooms(unittest.TestCase):
    """Friends lock in moves; nobody sees a locked move until they've locked theirs or the move is played."""

    def setUp(self):
        self.r = watcher.Room("abc234")

    def lock(self, cid, san, uci, ply=10, name=None):
        return self.r.apply(cid, name or cid, {"type": "lock", "game": "G1", "ply": ply, "san": san, "uci": uci})

    def test_locked_moves_stay_hidden_until_you_lock_or_it_is_played(self):
        self.lock("alice123", "Nf3", "g1f3")
        seen_by_bob = self.r.view_picks("bob12345")["G1"]["10"]
        self.assertEqual(seen_by_bob, [{"name": "alice123", "me": False}])  # bob sees that alice locked in, not what
        self.assertEqual(self.r.view_picks("alice123")["G1"]["10"][0]["san"], "Nf3")
        self.lock("bob12345", "d4", "d2d4")
        self.assertEqual({x["san"] for x in self.r.view_picks("bob12345")["G1"]["10"]}, {"Nf3", "d4"})
        self.r.apply("carol123", "carol", {"type": "played", "game": "G1", "ply": 11})
        self.assertEqual(self.r.view_picks("carol123")["G1"]["10"][0]["san"], "Nf3")  # played: everyone sees

    def test_a_lock_is_final_and_too_late_once_played(self):
        self.lock("alice123", "Nf3", "g1f3")
        with self.assertRaises(ValueError):
            self.lock("alice123", "d4", "d2d4")
        self.r.apply("alice123", "alice", {"type": "played", "game": "G1", "ply": 11})
        with self.assertRaises(ValueError):
            self.lock("bob12345", "d4", "d2d4")

    def test_played_only_moves_forward(self):
        self.assertEqual(self.r.apply("a1234567", "a", {"type": "played", "game": "G1", "ply": 5}), "picks")
        self.assertIsNone(self.r.apply("a1234567", "a", {"type": "played", "game": "G1", "ply": 3}))
        self.assertEqual(self.r.played["G1"], 5)

    def test_chat_and_bad_changes(self):
        self.r.apply("a1234567", "Ann", {"type": "chat", "text": "  what a move  ", "ply": 12})
        self.assertEqual(self.r.chat[-1]["text"], "what a move")
        for body in ({"type": "chat", "text": "  "}, {"type": "lock", "game": "G1", "ply": 1, "uci": "zz", "san": "x"},
                     {"type": "game", "game": {"id": "x", "url": "https://example.com"}}, {"type": "nope"}):
            with self.assertRaises(ValueError):
                self.r.apply("a1234567", "Ann", body)
        for i in range(watcher.MAX_CHAT + 5):
            self.r.apply("a1234567", "Ann", {"type": "chat", "text": str(i)})
        self.assertEqual(len(self.r.chat), watcher.MAX_CHAT)


class RoomEndpoints(Isolated):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(watcher, "ROOMS_FILE", os.path.join(self.tmp.name, "rooms.json"))
        p.start(); self.addCleanup(p.stop)
        watcher.rooms.clear()
        self.srv = watcher.http.server.ThreadingHTTPServer(("127.0.0.1", 0), watcher.Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.srv.server_address[1], timeout=5)

    def post(self, path, body, host=None):
        c = self.conn()
        h = {"Content-Type": "application/json"}
        if host:
            h["Host"] = host
        c.request("POST", path, body=json.dumps(body), headers=h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or "null")

    def get(self, path):
        c = self.conn()
        c.request("GET", path)
        r = c.getresponse()
        return r.status, json.loads(r.read() or "null")

    def test_two_friends_in_a_room(self):
        rid = self.post("/rooms", {})[1]["id"]
        self.assertRegex(rid, r"^[a-z0-9]{6}$")
        status, bob = self.get(f"/room/{rid}/poll?cid=bob12345&name=Bob")  # first ask: answers at once
        self.assertEqual((status, bob["people"], bob["chat"]), (200, ["Bob"], []))

        # Bob waits for news; Alice's message answers his waiting request straight away
        got = {}
        waiting = threading.Thread(target=lambda: got.update(self.get(
            f"/room/{rid}/poll?cid=bob12345&name=Bob&since={bob['version']}&after=0")[1]))
        waiting.start()
        self.assertEqual(self.post(f"/room/{rid}", {"cid": "alice123", "name": "Alice", "type": "chat", "text": "hi"})[0], 200)
        waiting.join(5)
        self.assertFalse(waiting.is_alive())
        self.assertEqual([m["text"] for m in got["chat"]], ["hi"])
        self.assertEqual(got["people"], ["Alice", "Bob"])

        self.post(f"/room/{rid}", {"cid": "alice123", "name": "Alice", "type": "lock", "game": "G1", "ply": 0, "san": "e4", "uci": "e2e4"})
        _, bob = self.get(f"/room/{rid}/poll?cid=bob12345&name=Bob&after={got['chat'][-1]['id']}")
        self.assertEqual(bob["picks"], {"G1": {"0": [{"name": "Alice", "me": False}]}})  # locked, but hidden
        self.assertEqual(bob["chat"], [])  # only messages after the last one he has
        status, body = self.post(f"/room/{rid}", {"cid": "alice123", "name": "Alice", "type": "lock", "game": "G1", "ply": 0, "san": "d4", "uci": "d2d4"})
        self.assertEqual((status, body["error"]), (400, "you already locked in this move"))
        with open(watcher.ROOMS_FILE) as fh:
            self.assertEqual(json.load(fh)[rid]["chat"][0]["text"], "hi")  # survives a restart

    def test_a_quiet_room_answers_after_the_wait(self):
        rid = self.post("/rooms", {})[1]["id"]
        _, v = self.get(f"/room/{rid}/poll?cid=bob12345&name=Bob")
        with mock.patch.object(watcher, "WAIT", 0.3):
            status, again = self.get(f"/room/{rid}/poll?cid=bob12345&name=Bob&since={v['version']}")
        self.assertEqual((status, again["version"]), (200, v["version"]))

    def test_people_who_leave_drop_off(self):
        r = watcher.Room("abc234")
        self.assertTrue(r.touch("a1234567", "Ann", 100))
        self.assertFalse(r.touch("a1234567", "Ann", 110))
        r.touch("b1234567", "Bob", 120)
        self.assertTrue(r.prune(110 + watcher.PRESENT + 1))
        self.assertEqual(r.people(), ["Bob"])

    def test_versions_keep_rising_across_restarts(self):
        old = watcher.Room("abc234")
        old.bump()
        self.assertGreaterEqual(watcher.Room("abc234", old.saved()).version, old.version - 1)

    def test_only_the_app_is_served(self):
        with open(os.path.join(self.tmp.name, "x"), "w"):
            pass
        for path, want in (("/", 200), ("/index.html", 200), ("/lib.js", 200), ("/vendor/chess.js", 200),
                           ("/rooms.json", 404), ("/follow.json", 404), ("/.git/config", 404),
                           ("/watcher.py", 404), ("/../etc/passwd", 404), ("/vendor/chess.js.LICENSE", 404)):
            c = self.conn()
            c.request("GET", path, headers={"Host": "funny-words.trycloudflare.com"})
            r = c.getresponse()
            r.read()
            self.assertEqual(r.status, want, path)

    def test_rooms_tell_the_page_the_public_link(self):
        with mock.patch.object(watcher, "public_url", "https://a-b.trycloudflare.com"):
            c = self.conn()
            c.request("GET", "/rooms")
            self.assertEqual(json.loads(c.getresponse().read())["public"], "https://a-b.trycloudflare.com")

    def test_room_caps(self):
        with mock.patch.object(watcher, "MAX_ROOMS", 1):
            self.assertEqual(self.post("/rooms", {})[0], 200)
            self.assertEqual(self.post("/rooms", {})[0], 503)
            self.assertEqual(self.post("/room/zzzzzz", {"cid": "alice123", "name": "A", "type": "chat", "text": "x"})[0], 503)

    def test_friends_through_a_tunnel_cannot_touch_your_popups(self):
        c = self.conn()
        c.request("GET", "/follow", headers={"Host": "funny-words.trycloudflare.com"})
        self.assertEqual(c.getresponse().status, 403)
        status, _ = self.post("/follow", {"remove": "x"}, host="funny-words.trycloudflare.com")
        self.assertEqual(status, 403)
        self.assertEqual(self.post("/rooms", {}, host="funny-words.trycloudflare.com")[0], 200)
        c = self.conn()  # a tunnel that keeps Host as localhost still marks the request as forwarded
        c.request("GET", "/follow", headers={"Cf-Connecting-Ip": "203.0.113.5"})
        self.assertEqual(c.getresponse().status, 403)


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
