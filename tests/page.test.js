// Checks on index.html that don't need a browser.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

test('keyboard handlers are never set with on…= (a handler returning false cancels the key)', () => {
  // This is how Backspace, Cmd+A and Cmd+V stopped working in the URL box.
  assert.doesNotMatch(page, /\.on(keydown|keypress|keyup|paste|input|beforeinput)\s*=/);
});
test('the page loads its logic from lib.js and chess.js from the repo, not a CDN', () => {
  assert.match(page, /from '\.\/lib\.js'/);
  assert.doesNotMatch(page, /cdn[^'"]*chess\.js/);
});
test('the page warns when it is opened as a file instead of from localhost', () => {
  assert.match(page, /location\.protocol === 'file:'/);
  assert.match(page, /window\.appReady = true/);
});
test('no engine: nothing loads Stockfish or the cloud eval', () => {
  assert.doesNotMatch(page, /stockfish|cloud-eval/i);
});
