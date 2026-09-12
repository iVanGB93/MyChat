const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../templates/monitor.html'), 'utf8');

test('monitor keeps working panels and removes retired placeholders', async () => {
  const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(m => m[1]);
  assert.equal(ids.length, new Set(ids).size, 'unique panel IDs');
  for (const id of ['msgsHour', 'msgs24h', 'unreadMsgs', 'totalMsgs', 'chatRoomsBody', 'recentBody']) {
    assert.ok(!ids.includes(id), id);
  }
  const elements = Object.fromEntries(ids.map(id => [id, {
    textContent: '', style: {}, innerHTML: '', classList: { remove() {}, add() {} },
  }]));
  let fail = false;
  const data = {
    server_time: new Date().toISOString(),
    websockets: { online_count: 0, background_count: 0, notification_count: 0, online_users: [], background_users: [] },
    users: { total: 2, with_push_token: 1, offline_with_push: 1 },
    rooms: { total: 1, direct: 1, group: 0 },
    calls: { active: [], active_count: 0, today_total: 0, today_missed: 0 },
    reliability: { scope: 'Retained delivery metadata', messages: { ack_rate_24h: null, sender_confirmation_pending: 0 }, calls: {} },
  };
  const context = vm.createContext({
    document: { hidden: false, addEventListener() {}, getElementById(id) { assert.ok(elements[id], `missing ${id}`); return elements[id]; } },
    fetch: async () => { if (fail) throw new Error('offline'); return { ok: true, json: async () => data }; },
    console: { error() {} }, AbortController, setTimeout: () => 1, clearTimeout() {}, setInterval() {},
  });
  const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace('\nrefresh();', '');
  vm.runInContext(script, context);
  await vm.runInContext('refresh()', context);
  assert.equal(elements.dataStatus.textContent, 'Current');
  assert.equal(elements.msgAckRate24h.textContent, 'No samples');
  fail = true;
  await vm.runInContext('refresh()', context);
  assert.match(elements.dataStatus.textContent, /STALE/);
});
