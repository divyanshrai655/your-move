# your-move

Follow live chess on Lichess and find the best move before the players do.

## Run

```bash
cd ~/your-move
python3 watcher.py
```

Open http://localhost:8765 and paste any Lichess broadcast or game link. Don't
open `index.html` directly: it has to be served from localhost.

## Features

- Find the best move for the side to play. When the real move lands, the engine
  grades yours (Best, Excellent, Good, Inaccuracy, Mistake, Blunder), shows the
  best move, and grades the player's move the same way.
- Pin a position, step through moves with ◀ ▶, and try your own lines.
- Live clocks, start times for games that haven't begun, and how often you found
  the best move.
- 🔔 a game to get a desktop popup on every move, even with the browser closed
  (macOS).
