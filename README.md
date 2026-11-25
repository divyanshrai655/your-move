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
- A link to one game follows just that game; a round link follows all of it. ✕
  drops a game.
- The whole game is listed on the right, with your move under each of the
  players'. Click a move to see that position.
- Pin a position to keep thinking while the game moves on, step through moves
  with ◀ ▶ or the arrow keys, and try your own lines.
- Live clocks and start times.

## Settings

**⚙** sets the board colours, the piece set, coordinates, and a sound when a move
lands. Piece sets are loaded from Lichess's repository and are all openly
licensed; each set's license is listed in `lib.js`.

## With friends

Your Mac is the server, so there is nothing to host or pay for. Start the
watcher with `--share` (once: `brew install cloudflared`):

```bash
python3 watcher.py --share
```

It prints a public link like `https://some-random-words.trycloudflare.com`. Then
in the app:

1. Open the game you want to watch and press **New room**.
2. Pick a name, press **Copy link**, and send that link to your friends. It
   opens the room and the game for them, with nothing to install.

In a room everyone watches the same game, chats, and locks in moves:

- Drag your move, then press **🔒 Lock in**. A locked move can't be changed.
- Friends see that you've locked in, but not what, until they lock in too or the
  player moves. Then everyone's moves show, with a running score.
- **Watch this game here** moves the room to the game you have open.

Good to know:

- The link only works while the watcher runs, and it changes every time you
  start it. Send the new one after a restart. Rooms and chat are kept in
  `rooms.json`.
- Through the link, friends get the app and its rooms and nothing else. They
  can't see your files or your 🔔 list.
- There are no accounts: anyone with the link can join under any name. Share it
  only with people you'd invite.

## Tests

```bash
./test.sh
```

Runs offline in a few seconds. It runs before every commit too, once you turn on
the hook in a fresh clone with `git config core.hooksPath .githooks`.
