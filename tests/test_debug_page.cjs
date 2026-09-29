// Run with: node --test tests/test_debug_page.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function page(responses) {
  const nodes = new Map();
  function element() {
    return { value: '', textContent: '', disabled: false, children: [],
      classList: { toggle() {} }, lastElementChild: { textContent: '' },
      addEventListener() {}, focus() {}, remove() {}, querySelector() { return null; },
      appendChild(child) { this.children.push(child); }, replaceChildren() { this.children = []; },
    };
  }
  const calls = [];
  const sandbox = { URL, TextDecoder, Uint8Array, AbortController, AbortSignal,
    crypto: webcrypto, location: { href: 'http://localhost/debug', origin: 'http://localhost' },
    document: { querySelector(id) { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); }, createElement: element },
    async fetch(url, options) { calls.push({ url: String(url), ...options }); return responses.shift(); },
  };
  vm.createContext(sandbox);
  const html = fs.readFileSync('app/debug.html', 'utf8');
  vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], sandbox);
  const run = code => vm.runInContext(code, sandbox);
  const login = async () => {
    run("elements.username.value = 'alice'; elements.password.value = 'password';");
    await run('login({ preventDefault() {} })');
  };
  return { run, nodes, calls, login };
}

test('login precedes chat, bearer token is sent, logout clears account context', async () => {
  const encoder = new TextEncoder();
  const p = page([
    { ok: true, json: async () => ({ token: 'test-token' }) },
    { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(c) { c.enqueue(encoder.encode('event: message\ndata: "回答"\n\nevent: done\ndata: {}\n\n')); c.close(); } }) },
  ]);
  assert.equal(p.nodes.get('#send').disabled, true);
  p.run("elements.question.value = '问题'");
  await p.run('sendMessage()');
  assert.equal(p.calls.length, 0);
  await p.login();
  assert.equal(p.calls[0].url, 'http://localhost/login');
  assert.equal(p.nodes.get('#password').value, '');
  assert.equal(p.nodes.get('#send').disabled, false);
  await p.run('sendMessage()');
  assert.equal(p.calls[1].headers.Authorization, 'Bearer test-token');
  assert.equal('user_id' in JSON.parse(p.calls[1].body), false);
  assert.equal(p.nodes.get('#progress').textContent, '本轮处理完成');
  const previousId = p.nodes.get('#conversationId').value;
  p.run('clearAuth()');
  assert.equal(p.nodes.get('#send').disabled, true);
  assert.equal(p.nodes.get('#messages').children.length, 0);
  assert.notEqual(p.nodes.get('#conversationId').value, previousId);
});

test('bad login remains locked; expired token requires login again', async () => {
  const p = page([{ ok: false, status: 401 }, { ok: true, json: async () => ({ token: 'token' }) }, { ok: false, status: 401 }]);
  await p.login();
  assert.equal(p.nodes.get('#send').disabled, true);
  assert.match(p.nodes.get('#authStatus').textContent, /用户名或密码错误/);
  await p.login();
  p.run("elements.question.value = '问题'");
  await p.run('sendMessage()');
  assert.equal(p.nodes.get('#send').disabled, true);
  assert.match(p.nodes.get('#authStatus').textContent, /重新登录/);
});

test('cross-origin endpoint does not receive credentials', async () => {
  const p = page([]);
  p.run("elements.endpoint.value = 'https://example.com/chat'");
  await p.login();
  assert.equal(p.calls.length, 0);
  assert.match(p.nodes.get('#authStatus').textContent, /同源/);
});
