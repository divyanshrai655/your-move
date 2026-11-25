// The app's logic, kept free of the page and the network so tests/lib.test.js can check it.
// index.html does the screen, the board and the Lichess connections, and calls these.
import { Chess } from './vendor/chess.js';
export { Chess };

// ---------- links ----------
const ID8 = /^[A-Za-z0-9]{8}$/;
// A pasted link -> { kind: 'round', id, gameId? } | { kind: 'game', id } | null
export function parseInput(s) {
  s = s.trim(); let u;
  try { u = new URL(s); } catch { return ID8.test(s) ? { kind: 'round', id: s } : null; }
  const p = u.pathname.split('/').filter(Boolean), b = p.indexOf('broadcast');
  if (b >= 0) return ID8.test(p[b + 3] || '') ? { kind: 'round', id: p[b + 3], gameId: p[b + 4] } : null;
  if (/^[A-Za-z0-9]{8,12}$/.test(p[0] || '')) return { kind: 'game', id: p[0].slice(0, 8) };
  return null;
}

// ---------- what you follow ----------
// A source is a round or a game. For a round, `only` lists the games you follow (null = all of them).
// A link to one game follows just that game; a link to the round follows every game in it.
// Returns what the page should do: start a 'new' source, 'restart' one whose games changed, or just 'select'.
export function addSource(sources, link) {
  const wantGame = link.gameId || (link.kind === 'game' ? link.id : null);
  const ex = sources.find(x => x.id === link.id);
  if (!ex) {
    const source = { kind: link.kind, id: link.id, only: link.gameId ? [link.gameId] : null };
    sources.push(source);
    return { action: 'new', source, wantGame };
  }
  if (ex.only && !link.gameId) { ex.only = null; return { action: 'restart', source: ex, wantGame }; }
  if (ex.only && !ex.only.includes(link.gameId)) { ex.only.push(link.gameId); return { action: 'restart', source: ex, wantGame }; }
  return { action: 'select', source: ex, wantGame };
}
export const follows = (source, gameId) => !source.only || source.only.includes(gameId);
// Stop following one game. `shownIds` are the ids of this source's games on screen.
// Returns true when nothing of the source is left, and removes it from `sources`.
export function dropGame(sources, source, gameId, shownIds) {
  if (source.kind === 'round') source.only = (source.only || shownIds).filter(id => id !== gameId);
  const empty = source.kind === 'game' || !source.only.length;
  if (empty) sources.splice(sources.indexOf(source), 1);
  return empty;
}

// ---------- 🔔 popups ----------
// bells: { gameId: { name, url } }. With watcher.py running, the watcher's list is the one that counts:
// the page shows it and sends one change at a time, so a second browser can't wipe it.
// Flips 🔔 for a game and returns the single change to send: { add: {id, name, url} } or { remove: id }.
export function toggleBell(bells, game) {
  if (bells[game.id]) { delete bells[game.id]; return { remove: game.id }; }
  bells[game.id] = { name: game.name, url: gameUrl(game) };
  return { add: { id: game.id, ...bells[game.id] } };
}
// Replace the page's bells with the watcher's list. Whole rounds named on the command line ("<round>:*") aren't games.
export function bellsFromWatcher(bells, follows) {
  for (const id of Object.keys(bells)) delete bells[id];
  for (const [id, f] of Object.entries(follows || {})) if (!id.endsWith(':*')) bells[id] = { name: f.name, url: f.url };
  return bells;
}

// ---------- reading Lichess ----------
export function readHeaders(text) {
  const h = {};
  for (const m of text.matchAll(/^\[(\w+) "(.*)"\]\s*$/gm)) h[m[1]] = m[2];
  return h;
}
// Which game a PGN from a broadcast stream is about (cheap: headers only).
export function pgnGameId(text, roundId) {
  if (!text.includes('[')) return null;
  const h = readHeaders(text);
  return (h.GameURL || '').split('/').pop() || `${roundId}:${h.White}-${h.Black}`;
}
// One game's PGN -> { id, name, result, over, ucis, sans, fens, clk } or null if it isn't a game.
// clk = seconds left for each side after their last move, from Lichess's "[%clk 1:30:42]" comments.
export function readPgn(text, roundId) {
  const id = pgnGameId(text, roundId); if (!id) return null;
  const h = readHeaders(text);
  const c = new Chess(); try { c.loadPgn(text); } catch { return null; }
  const r = new Chess(h.FEN || undefined), fens = [r.fen()], ucis = [], sans = [], clk = {};
  const notes = new Map(c.getComments().map(x => [x.fen, x.comment]));
  for (const m of c.history({ verbose: true })) {
    r.move(m.san); fens.push(r.fen()); ucis.push(toUci(m)); sans.push(m.san);
    const t = /%clk (\d+):(\d+):(\d+(?:\.\d+)?)/.exec(notes.get(r.fen()) || '');
    if (t) clk[m.color] = t[1] * 3600 + t[2] * 60 + +t[3];
  }
  const result = h.Result || '*';
  return { id, name: `${h.White} – ${h.Black}`, result, over: result !== '*', ucis, sans, fens, clk };
}
// A game-info line from a lichess.org game stream (first line, and again at the end with the result).
export function readGameInfo(o) {
  const pl = c => o.players?.[c]?.user?.name || (o.players?.[c]?.aiLevel && `Stockfish ${o.players[c].aiLevel}`) || c;
  const st = o.status?.name || o.status, over = !!st && !['created', 'started'].includes(st);
  return { name: `${pl('white')} – ${pl('black')}`, over,
    result: over ? (o.winner === 'white' ? '1-0' : o.winner === 'black' ? '0-1' : '½-½') : '*' };
}
// A move line from a game stream. `g` holds { ucis, sans, fens, ri } and is updated in place.
// Returns the new move lists, or null when nothing is new (Lichess replays the whole game on every connect).
export function readTick(g, o) {
  if (!o.fen) return null;
  if (o.wc != null) g.clk = { w: o.wc, b: o.bc };
  if (!o.lm) { if (!g.ucis.length) g.fens = [o.fen]; g.ri = 0; return null; }
  if (g.ri++ < g.ucis.length) return null;
  const m = playUci(g.fens.at(-1), o.lm);
  return { ucis: [...g.ucis, m ? toUci(m) : o.lm], sans: [...g.sans, m ? m.san : o.lm], fens: [...g.fens, o.fen] };
}
// Lichess may write castling as king-takes-rook (e1h1); chess.js wants e1g1.
const CASTLE = { e1h1: 'e1g1', e1a1: 'e1c1', e8h8: 'e8g8', e8a8: 'e8c8' };
export function playUci(fen, u) {
  const c = new Chess(fen);
  for (const x of [u, CASTLE[u.slice(0, 4)]]) if (x) try { return c.move({ from: x.slice(0, 2), to: x.slice(2, 4), promotion: x[4] }); } catch {}
  return null;
}
export const toUci = m => m.from + m.to + (m.promotion || '');

// ---------- your moves ----------
// guesses: { ply: { uci, san } }. Fills in what was played for every ply the game has reached.
// Returns the plies that were just revealed.
export function revealGuesses(guesses, ucis, sans) {
  const out = [];
  for (const [p, x] of Object.entries(guesses || {})) {
    if (x.actual || +p >= ucis.length) continue;
    Object.assign(x, { actual: ucis[p], actualSan: sans[p], match: x.uci === ucis[p] });
    out.push(+p);
  }
  return out;
}

// The whole game as rows for the move list: [{ no, cells: [white, black] }].
// A cell is { ply, san, mine?, match? } (your move shows once the real one is in), { ply, pending } for the
// move you've picked but the player hasn't made yet, or null.
export function moveRows(sans, guesses, over = false) {
  const cells = sans.map((san, ply) => {
    const x = guesses?.[ply];
    return x?.actual ? { ply, san, mine: x.san, match: x.match } : { ply, san };
  });
  const next = guesses?.[sans.length];
  if (next && !over) cells.push({ ply: sans.length, pending: next.san });
  const rows = [];
  for (let i = 0; i < cells.length; i += 2) rows.push({ no: i / 2 + 1, cells: [cells[i], cells[i + 1] || null] });
  return rows;
}

// ---------- rooms ----------
// Lichess link for one game, as rooms and 🔒 popups pass it around.
export const gameUrl = g => g.src.kind === 'round' ? `https://lichess.org/broadcast/-/-/${g.src.id}/${g.id}` : `https://lichess.org/${g.id}`;
// Friends' locked moves for one game ({ply: [{name, me, san?, uci?}]}) scored against what was played:
// [{name, me, hits, of}], best first. Moves still hidden or not yet played don't count.
export function roomScores(picks, ucis) {
  const by = new Map();
  for (const [p, list] of Object.entries(picks || {})) {
    if (+p >= ucis.length) continue;
    for (const x of list) {
      if (!x.uci) continue;
      const s = by.get(x.name) || { name: x.name, me: false, hits: 0, of: 0 };
      s.me ||= x.me; s.of++; if (x.uci === ucis[p]) s.hits++;
      by.set(x.name, s);
    }
  }
  return [...by.values()].sort((a, b) => b.hits - a.hits || a.of - b.of || a.name.localeCompare(b.name));
}
export const newCid = () => Array.from(crypto.getRandomValues(new Uint8Array(12)), b => 'abcdefghijklmnopqrstuvwxyz0123456789'[b % 36]).join('');
export const parseRoomHash = h => (/^#room=([a-z0-9]{6})$/.exec(h || '') || [])[1] || null;

// ---------- look ----------
// Board squares: [light, dark].
export const BOARDS = {
  brown: ['#f0d9b5', '#b58863'], green: ['#eeeed2', '#769656'], blue: ['#dee3e6', '#8ca2ad'],
  ice: ['#e8f1f7', '#7ea6c4'], coral: ['#f4e4d4', '#cf8f6c'], purple: ['#e8e0f0', '#9b7fb6'],
  slate: ['#c9ced6', '#6b7686'], night: ['#7d8796', '#3f4754'],
};
// Piece sets, all openly licensed (from Lichess's repository, which lists each set's license in COPYING.md).
export const PIECES = {
  cburnett: 'GPLv2+, Colin M.L. Burnett', merida: 'GPLv2+, Armando Hernandez Marroquin',
  chessnut: 'Apache 2.0, Alexis Luengas', fantasy: 'MIT, Maurizio Monge', spatial: 'MIT, Maurizio Monge',
  celtic: 'MIT, Maurizio Monge', rhosgfx: 'CC0 1.0, RhosGFX',
};
const LILA = 'https://cdn.jsdelivr.net/gh/lichess-org/lila@39fe88107d406839d94052ae8f9958423d7c3fc3/public/piece';
export const pieceUrl = (set, code) => `${LILA}/${set}/${code}.svg`; // code like 'wN'
// CSS that swaps chessground's pieces for another set ('' keeps the built-in cburnett).
export function pieceCss(set) {
  if (!PIECES[set] || set === 'cburnett') return '';
  const roles = { pawn: 'P', knight: 'N', bishop: 'B', rook: 'R', queen: 'Q', king: 'K' };
  return Object.entries(roles).flatMap(([role, r]) => ['white', 'black'].map(col =>
    `.cg-wrap piece.${role}.${col}{background-image:url('${pieceUrl(set, col[0] + r)}')}`)).join('\n');
}
export const DEFAULT_SETTINGS = { board: 'brown', pieces: 'cburnett', coords: true, sound: true };
export const readSettings = s => {
  const o = { ...DEFAULT_SETTINGS, ...(s && typeof s === 'object' ? s : {}) };
  if (!BOARDS[o.board]) o.board = DEFAULT_SETTINGS.board;
  if (!PIECES[o.pieces]) o.pieces = DEFAULT_SETTINGS.pieces;
  return o;
};

// ---------- formatting ----------
export const moveNo = p => p % 2 ? `${(p + 1) / 2}…` : `${p / 2 + 1}.`;
export function fmtClock(t) {
  if (t == null) return '–:––';
  t = Math.floor(t); const h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, s = String(t % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}
export function fmtStart(ts, now = Date.now()) {
  const mins = Math.round((ts - now) / 60000), when = new Date(ts).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  return mins > 0 ? `${when} (in ${mins >= 60 ? `${Math.floor(mins / 60)}h ` : ''}${mins % 60}m)` : when;
}
