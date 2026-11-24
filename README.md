# your-move

Follow live chess move by move and guess what the players will do next.

Watching a championship with friends is mostly asking each other "what would you
play here?". Your Move asks you that every time a move lands in a game you're
following, then shows whether you matched the player and what the engine thought.

## Run it

```bash
cd ~/your-move
python3 watcher.py
```

Then open http://localhost:8765 and paste any Lichess broadcast or game link.

- Press 🔔 on the games you care about. From then on, every new move in those games
  pops up a desktop dialog ("29. Nb6+ · Black to move. What would you play?"), even
  with the browser closed. **Open board** jumps to that game.
- The games you've belled are saved in `follow.json`, so restarting the watcher keeps
  them.
- `--open` opens the board immediately instead of asking. `--print` only logs moves
  in the terminal.
- You can also pass links on the command line: `python3 watcher.py <url> ...`.
- Popups need macOS. On other systems the moves are printed in the terminal.

The page also works without the watcher (`python3 -m http.server 8765`). Then 🔔 uses
browser notifications, which only work while the tab is open.

## What the web app does

- **Many games at once.** Paste any number of broadcast rounds or game URLs. Games
  that haven't started show their start time.
- **Guess the move.** On the live position, drag the move you expect. It locks. When
  the real move arrives you see ✓/✗, plus the Lichess cloud eval and best move
  (when Lichess has one).
- **Pin.** Hold a position while the game moves on. ◀ ▶ (or the arrow keys) step
  through the game. Drag pieces to try your own lines. **Catch up** (or ⏭ / End)
  returns to live.
- **Clocks.** Both players' clocks, counting down for the side to move.
- **Score.** At the end of a game you get your guesses vs the real moves and your
  match rate. Everything is saved in localStorage.
- **Deep links.** `http://localhost:8765/#<lichess url>` opens straight to a game.

## How moves arrive

There's no polling. Lichess keeps one HTTP response open and writes to it whenever
a move is played:

- broadcast rounds: `/api/stream/broadcast/round/{id}.pgn`. One connection carries
  every game in the round, and each update is the full PGN of the game that changed.
- games: `/api/stream/game/{id}`. One JSON line per move.

A broadcast stream is silent once a round is over, so the app first loads a
snapshot (`/api/broadcast/round/{id}.pgn`). After that it only reconnects when the
connection drops, waiting longer after repeated failures (61 s after a 429).

## Where it's going

Friends' rooms with chat and shared guesses need a small relay server. The server
holds one Lichess stream per round, no matter how many people watch, and passes
each new move to every browser in a room over WebSocket.
