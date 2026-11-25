// Tests for lib.js: run with `node --test tests/` (or ./test.sh). Offline: uses saved Lichess data in fixtures/.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import {
  Chess, parseInput, addSource, dropGame, follows, pgnGameId, readPgn, readGameInfo, readTick,
  playUci, revealGuesses, toggleBell, bellsFromWatcher, moveNo, fmtClock, fmtStart,
  moveRows, BOARDS, PIECES, pieceCss, readSettings, roomScores, parseRoomHash, gameUrl,
} from '../lib.js';

const fixture = name => readFileSync(new URL(`./fixtures/${name}`, import.meta.url), 'utf8');
const ROUND = fixture('round.pgn').split('\n\n\n').filter(t => t.includes('['));
const STREAM = fixture('game-stream.ndjson').split('\n').filter(Boolean).map(l => JSON.parse(l));

// ---------- links ----------
test('a broadcast link to one game gives the round and that game', () => {
  assert.deepEqual(
    parseInput('https://lichess.org/broadcast/2026-chinese-chess-league-division-a-finals/round-1-game-2/5qZNwCTF/EdbhF3R2'),
    { kind: 'round', id: '5qZNwCTF', gameId: 'EdbhF3R2' });
});
test('a broadcast round link has no game', () => {
  assert.deepEqual(parseInput('https://lichess.org/broadcast/fide-world-chess-championship-2026/game-1/xLgOJ6cb'),
    { kind: 'round', id: 'xLgOJ6cb', gameId: undefined });
});
test('a lichess.org game link (with or without the player suffix) is a game', () => {
  assert.deepEqual(parseInput('https://lichess.org/1Rs5kLFb'), { kind: 'game', id: '1Rs5kLFb' });
  assert.deepEqual(parseInput('https://lichess.org/1Rs5kLFbABCD'), { kind: 'game', id: '1Rs5kLFb' });
});
test('spaces around a pasted link are ignored', () => {
  assert.equal(parseInput('  https://lichess.org/1Rs5kLFb \n').id, '1Rs5kLFb');
});
test('other links and text are rejected', () => {
  for (const s of ['https://example.com/hello', 'hello', '', 'https://lichess.org/broadcast/x/y']) assert.equal(parseInput(s), null, s);
});

// ---------- following one game vs the whole round ----------
const link = s => parseInput(`https://lichess.org/broadcast/t/r/${s}`);
test('a link to one game follows only that game', () => {
  const sources = [];
  const r = addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  assert.equal(r.action, 'new');
  assert.equal(r.wantGame, 'EdbhF3R2');
  assert.deepEqual(sources, [{ kind: 'round', id: '5qZNwCTF', only: ['EdbhF3R2'] }]);
  assert.ok(follows(sources[0], 'EdbhF3R2'));
  assert.ok(!follows(sources[0], 'Y4Aas90z'));
});
test('a second game from the same round is added, not a second connection', () => {
  const sources = [];
  addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  const r = addSource(sources, link('5qZNwCTF/Y4Aas90z'));
  assert.equal(r.action, 'restart');
  assert.equal(sources.length, 1);
  assert.deepEqual(sources[0].only, ['EdbhF3R2', 'Y4Aas90z']);
});
test('a round link follows every game in the round', () => {
  const sources = [];
  addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  assert.equal(addSource(sources, link('5qZNwCTF')).action, 'restart');
  assert.equal(sources[0].only, null);
  assert.ok(follows(sources[0], 'anything'));
});
test('pasting a game you already follow just opens it', () => {
  const sources = [];
  addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  const r = addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  assert.equal(r.action, 'select');
  assert.equal(r.wantGame, 'EdbhF3R2');
});
test('dropping one game from a whole round keeps the others', () => {
  const sources = []; addSource(sources, link('5qZNwCTF'));
  const removed = dropGame(sources, sources[0], 'B', ['A', 'B', 'C']);
  assert.equal(removed, false);
  assert.deepEqual(sources[0].only, ['A', 'C']);
});
test('dropping the last followed game of a round removes the round', () => {
  const sources = []; addSource(sources, link('5qZNwCTF/EdbhF3R2'));
  assert.equal(dropGame(sources, sources[0], 'EdbhF3R2', ['EdbhF3R2']), true);
  assert.deepEqual(sources, []);
});

// ---------- reading a broadcast round ----------
test('each game in a round PGN gets its id from the GameURL', () => {
  assert.deepEqual(ROUND.map(t => pgnGameId(t, '5qZNwCTF')), ['SWxMuX4D', 'EdbhF3R2', 'j9bPfDPt']);
});
test('a finished game is read with its moves and result', () => {
  const g = readPgn(ROUND[0], '5qZNwCTF');
  assert.equal(g.name, 'Chen, Qi b – Huang, Renjie');
  assert.equal(g.result, '1/2-1/2');
  assert.equal(g.over, true);
  assert.equal(g.sans[10], 'Bd3');
  assert.equal(g.fens.length, g.ucis.length + 1);
  assert.equal(new Chess(g.fens.at(-1)).fen(), g.fens.at(-1)); // the last position is a real position
});
test('a game still being played is not over', () => {
  const g = readPgn(ROUND[1], '5qZNwCTF');
  assert.equal(g.result, '*');
  assert.equal(g.over, false);
});
test('clocks come from the %clk comments, in seconds', () => {
  const g = readPgn(ROUND[1], '5qZNwCTF');
  assert.ok(g.clk.w > 0 && g.clk.b > 0);
  const text = '[White "A"]\n[Black "B"]\n[Result "*"]\n\n1. e4 { [%clk 1:30:42] } 1... e5 { [%clk 0:05:09.5] } *';
  assert.deepEqual(readPgn(text, 'r').clk, { w: 5442, b: 309.5 });
});
test('a game that has not started has no moves but is still a game', () => {
  const g = readPgn('[White "Sindarov"]\n[Black "Gukesh"]\n[Result "*"]\n[GameURL "https://lichess.org/broadcast/a/b/xLgOJ6cb/j7pdr7s2"]\n\n *', 'xLgOJ6cb');
  assert.equal(g.id, 'j7pdr7s2');
  assert.deepEqual(g.ucis, []);
  assert.equal(g.over, false);
});
test('text without PGN headers is ignored', () => {
  assert.equal(pgnGameId('\n\n', 'r'), null);
  assert.equal(readPgn('', 'r'), null);
});

// ---------- reading a lichess.org game stream ----------
function replay(lines, g = { ucis: [], sans: [], fens: [], ri: 0 }) {
  let added = 0;
  for (const o of lines) {
    if (o.id) continue;
    const next = readTick(g, o);
    if (next) { Object.assign(g, next); added++; }
  }
  return { g, added };
}
test('a game stream is read move by move', () => {
  const { g, added } = replay(STREAM);
  assert.equal(added, g.ucis.length);
  assert.ok(g.ucis.length > 20);
  assert.ok(g.sans.includes('O-O'));
});
test('history replayed after a reconnect is not counted as new moves', () => {
  const { g } = replay(STREAM);
  assert.equal(replay(STREAM, g).added, 0);
});
test('the final game-info line says the game is over and who won', () => {
  const info = readGameInfo(STREAM.filter(o => o.id).at(-1));
  assert.deepEqual(info, { name: 'Hoptop2007 – xurramid77', over: true, result: '0-1' });
  assert.equal(readGameInfo(STREAM[0]).over, false);
});
test("Lichess's king-takes-rook castling (e1h1) is understood", () => {
  const fen = 'r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1';
  assert.equal(playUci(fen, 'e1h1').san, 'O-O');
  assert.equal(playUci(fen, 'e1g1').san, 'O-O');
  assert.equal(playUci(fen, 'e1a1').san, 'O-O-O');
  assert.equal(playUci(fen, 'a1a9'), null);
});

// ---------- 🔔 popups ----------
const game = (id, src) => ({ id, name: 'A – B', src });
test('🔔 on sends one add, 🔔 off sends one remove, never the whole list', () => {
  const bells = {};
  assert.deepEqual(toggleBell(bells, game('G1', { kind: 'round', id: '5qZNwCTF' })),
    { add: { id: 'G1', name: 'A – B', url: 'https://lichess.org/broadcast/-/-/5qZNwCTF/G1' } });
  assert.deepEqual(toggleBell(bells, game('1Rs5kLFb', { kind: 'game', id: '1Rs5kLFb' })).add.url, 'https://lichess.org/1Rs5kLFb');
  assert.deepEqual(toggleBell(bells, game('G1', { kind: 'round', id: '5qZNwCTF' })), { remove: 'G1' });
  assert.deepEqual(Object.keys(bells), ['1Rs5kLFb']);
});
test("the watcher's list replaces the page's bells, so every browser shows the same", () => {
  const bells = { OLD: { name: 'x', url: 'y' } };
  bellsFromWatcher(bells, { G1: { name: 'A – B', url: 'u1' }, '5qZNwCTF:*': { name: '', url: 'u2', cli: true } });
  assert.deepEqual(bells, { G1: { name: 'A – B', url: 'u1' } });
});
test('the page only ever sends single changes to the watcher', () => {
  const page = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  assert.match(page, /tellWatcher\(toggleBell\(/);
  assert.doesNotMatch(page, /JSON\.stringify\(list\)/);
});

// ---------- your moves ----------
test('your move is revealed next to the real one once it is played, not before', () => {
  const guesses = { 0: { uci: 'e2e4', san: 'e4' }, 1: { uci: 'c7c5', san: 'c5' } };
  assert.deepEqual(revealGuesses(guesses, ['e2e4'], ['e4']), [0]);
  assert.equal(guesses[0].match, true);
  assert.equal(guesses[1].actual, undefined);
  assert.deepEqual(revealGuesses(guesses, ['e2e4', 'e7e5'], ['e4', 'e5']), [1]);
  assert.deepEqual(guesses[1], { uci: 'c7c5', san: 'c5', actual: 'e7e5', actualSan: 'e5', match: false });
  assert.deepEqual(revealGuesses(guesses, ['e2e4', 'e7e5'], ['e4', 'e5']), []); // nothing twice
});
test('there is no engine: revealing never adds a grade or eval', () => {
  const guesses = { 0: { uci: 'd2d4', san: 'd4' } };
  revealGuesses(guesses, ['e2e4'], ['e4']);
  assert.deepEqual(Object.keys(guesses[0]).sort(), ['actual', 'actualSan', 'match', 'san', 'uci']);
});

// ---------- formatting ----------
test('move numbers', () => {
  assert.equal(moveNo(0), '1.');
  assert.equal(moveNo(1), '1…');
  assert.equal(moveNo(46), '24.');
});
test('clocks', () => {
  assert.equal(fmtClock(null), '–:––');
  assert.equal(fmtClock(9), '0:09');
  assert.equal(fmtClock(5442), '1:30:42');
});
test('start times say how long to wait', () => {
  const now = Date.UTC(2026, 10, 24, 8, 34);
  assert.match(fmtStart(now + (5 * 60 + 26) * 60000, now), /\(in 5h 26m\)$/);
  assert.doesNotMatch(fmtStart(now - 60000, now), /in /);
});

// ---------- move list ----------
test('the move list pairs moves into numbered rows and shows your move under the player\'s', () => {
  const g = { 0: { uci: 'e2e4', san: 'e4' }, 1: { uci: 'c7c5', san: 'c5' }, 3: { uci: 'b8c6', san: 'Nc6' } };
  revealGuesses(g, ['e2e4', 'e7e5', 'g1f3'], ['e4', 'e5', 'Nf3']);
  const rows = moveRows(['e4', 'e5', 'Nf3'], g);
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0], { no: 1, cells: [{ ply: 0, san: 'e4', mine: 'e4', match: true }, { ply: 1, san: 'e5', mine: 'c5', match: false }] });
  // your pick for the next move waits at the end, until the player moves
  assert.deepEqual(rows[1].cells, [{ ply: 2, san: 'Nf3' }, { ply: 3, pending: 'Nc6' }]);
  assert.equal(moveRows(['e4', 'e5', 'Nf3'], g, true)[1].cells[1], null); // not once the game is over
  assert.deepEqual(moveRows([], undefined), []);
});

// ---------- look ----------
test('settings fall back to defaults for unknown or broken saved values', () => {
  assert.deepEqual(readSettings(null), { board: 'brown', pieces: 'cburnett', coords: true, sound: true });
  assert.deepEqual(readSettings({ board: 'nope', pieces: 'gone', coords: false }), { board: 'brown', pieces: 'cburnett', coords: false, sound: true });
  assert.equal(readSettings({ board: 'green' }).board, 'green');
});
test('every piece set names its license, and swapping sets covers all twelve pieces', () => {
  for (const [k, lic] of Object.entries(PIECES)) assert.match(lic, /GPL|MIT|Apache|CC0|CC BY-SA/, k);
  assert.equal(pieceCss('cburnett'), '');
  assert.equal(pieceCss('merida').split('\n').length, 12);
  assert.match(pieceCss('merida'), /piece\.knight\.black\{background-image:url\('[^']+\/merida\/bN\.svg'\)/);
  for (const [l, d] of Object.values(BOARDS)) { assert.match(l, /^#[0-9a-f]{6}$/); assert.match(d, /^#[0-9a-f]{6}$/); }
});

// ---------- rooms ----------
test('room scores count friends\' locked moves that matched, and skip hidden or unplayed ones', () => {
  const picks = {
    0: [{ name: 'Ann', me: true, san: 'e4', uci: 'e2e4' }, { name: 'Bob', me: false, san: 'd4', uci: 'd2d4' }],
    1: [{ name: 'Ann', me: true, san: 'c5', uci: 'c7c5' }, { name: 'Bob', me: false, san: 'e5', uci: 'e7e5' }],
    2: [{ name: 'Bob', me: false }], // still hidden
  };
  assert.deepEqual(roomScores(picks, ['e2e4', 'e7e5']), [
    { name: 'Ann', me: true, hits: 1, of: 2 }, { name: 'Bob', me: false, hits: 1, of: 2 }]);
  assert.deepEqual(roomScores(picks, ['e2e4']).map(s => [s.name, s.hits, s.of]), [['Ann', 1, 1], ['Bob', 0, 1]]);
  assert.deepEqual(roomScores(undefined, []), []);
});
test('room links', () => {
  assert.equal(parseRoomHash('#room=ab3dk9'), 'ab3dk9');
  assert.equal(parseRoomHash('#room=AB3'), null);
  assert.equal(parseRoomHash('#https://lichess.org/abcdefgh'), null);
  assert.equal(gameUrl({ id: 'G1', src: { kind: 'round', id: 'R1' } }), 'https://lichess.org/broadcast/-/-/R1/G1');
  assert.equal(gameUrl({ id: 'abcdefgh', src: { kind: 'game', id: 'abcdefgh' } }), 'https://lichess.org/abcdefgh');
});
