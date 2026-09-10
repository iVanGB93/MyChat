const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/app.html'), 'utf8');

test('web client scripts parse and use only one Axion socket', () => {
  for (const match of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) {
    new vm.Script(match[1]);
  }
  assert.equal((html.match(/new WebSocket\(/g) || []).length, 1);
  assert.ok(html.includes('/ws/notifications/'));
  assert.ok(!html.includes('/ws/chat/'));
});

test('visible browser reports active presence and requests pending rooms', () => {
  const sent = [];
  const context = {
    document: { hidden: false, getElementById: () => ({}) },
    WebSocket: { OPEN: 1 }, notifWsAuthenticated: true,
    notifWs: { readyState: 1, send: value => sent.push(JSON.parse(value)) },
    activeRoomId: 'current', recoveryRooms: new Set(['pending']),
  };
  const source = html.match(/    function refreshWebPresence\(\) \{[\s\S]*?\n    \}/)[0];
  vm.runInNewContext(source + '\nrefreshWebPresence();', context);
  assert.deepEqual(sent.map(frame => frame.type), ['app_state', 'ping', 'room_ready', 'room_ready']);
  assert.equal(sent[0].state, 'active');
  assert.equal(context.recoveryRooms.size, 0);
  sent.length = 0;
  context.document.hidden = true;
  vm.runInNewContext('refreshWebPresence();', context);
  assert.deepEqual(sent.map(frame => frame.type), ['app_state', 'ping']);
  assert.equal(sent[0].state, 'background');
});
