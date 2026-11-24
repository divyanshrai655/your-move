# your-move

Follow live chess on Lichess the way you would from the hall: think, pick your
move, then see what the player did. No engines on purpose.

## Run

```bash
cd ~/your-move
python3 watcher.py
```

Open http://localhost:8765 and paste any Lichess broadcast or game link. Don't
open `index.html` directly: it has to be served from localhost.

## Popups

Press 🔔 on any game. On every new move, a window pops up on your Mac asking
"What would you play?", even with the browser closed. **Open board** takes you
straight to the position.

## On the board

- Drag the move you'd play. Change it as often as you like until the real move
  lands, then see your move next to theirs.
- Pin a position to keep thinking while the game moves on, step through moves
  with ◀ ▶, and try your own lines.
- Live clocks and start times. At the end of a game you get your moves next to
  the players'.
