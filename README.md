# your-move

Follow live chess move by move and guess what the players will do next.

Watching a championship with friends is mostly asking each other "what would you
play here?". Your Move asks you that every time a move lands in a game you're
following, then shows whether you matched the player and what the engine thought.

## Run it

```bash
python3 watcher.py "https://lichess.org/broadcast/<tour>/<round>/<roundId>/<gameId>"
```

This serves the app at http://localhost:8765 and pops up a desktop dialog on every
new move, even with no browser open. Choose **Open board** to jump to that game.

- A broadcast URL ending in a game id watches that one game. A round URL watches
  every game in the round.
- A normal `https://lichess.org/<gameId>` URL works too.
- Add `--open` to open the board immediately instead of asking.
- Popups need macOS. On other systems the moves are printed in the terminal.

To use only the web app:

```bash
python3 -m http.server 8765
```

Then open http://localhost:8765 and paste a URL.

## What the web app does

- **Many games at once.** Paste any number of broadcast rounds or game URLs. 🔔 on
  a game sends a browser notification for each of its moves.
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
