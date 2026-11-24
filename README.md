# your-move

Follow live chess on Lichess and guess every move before the players make it.

## Run

```bash
cd ~/your-move
python3 watcher.py
```

Open http://localhost:8765 and paste any Lichess broadcast or game link. Don't
open `index.html` directly: it has to be served from localhost.

## Features

- Guess the next move, then see ✓/✗ and the engine's view.
- Pin a position, step through moves with ◀ ▶, and try your own lines.
- Live clocks, start times for games that haven't begun, and your match rate at
  the end of a game.
- 🔔 a game to get a desktop popup on every move, even with the browser closed
  (macOS).
