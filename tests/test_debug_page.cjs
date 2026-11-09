// Run with: node --test tests/test_debug_page.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

function page(responses) {
  const nodes = new Map();
  function element() {
    return { value: '', textContent: '', disabled: false, children: [], listeners: {},
      classList: { toggle() {} }, lastElementChild: { textContent: '' },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      focus() {}, remove() {}, querySelector() { return null; },
      prepend(child) { this.children.unshift(child); },
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
  const runId = webcrypto.randomUUID();
  let stream;
  const p = page([
    { ok: true, json: async () => ({ token: 'test-token' }) },
    { ok: true, status: 202, json: async () => ({ run_id: runId }) },
    { ok: true, headers: new Headers({ 'Content-Type': 'text/event-stream' }),
      body: new ReadableStream({ start(controller) {
        stream = controller;
        stream.enqueue(encoder.encode(': keep-alive\n\nevent: status\ndata: "正在查询"\n\nevent: message\ndata: "中间文本"\n\n'));
      } }) },
  ]);
  assert.equal(p.nodes.get('#send').disabled, true);
  p.run("elements.question.value = '问题'");
  await p.run('sendMessage()');
  assert.equal(p.calls.length, 0);
  await p.login();
  assert.equal(p.calls[0].url, 'http://localhost/login');
  assert.equal(p.nodes.get('#password').value, '');
  assert.equal(p.nodes.get('#send').disabled, false);
  const keydown = p.nodes.get('#question').listeners.keydown;
  let prevented = false;
  const enter = { key: 'Enter', preventDefault() { prevented = true; } };
  keydown({ ...enter, isComposing: true });
  keydown({ ...enter, shiftKey: true });
  assert.equal(prevented, false);
  assert.equal(p.calls.length, 1);
  const request = keydown(enter);
  assert.equal(prevented, true);
  await new Promise(resolve => setImmediate(resolve));
  const messages = p.nodes.get('#messages').children;
  assert.equal(messages.length, 2);
  const thinking = messages[1];
  assert.equal(thinking.open, true);
  assert.equal(thinking.children[0].textContent, 'Analysis Progress');
  assert.equal(thinking.children[1].textContent, '正在查询\n\n中间文本');
  stream.enqueue(encoder.encode('event: message\ndata: "继续更新"\n\n'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(messages.length, 2);
  assert.equal(thinking.children[1].textContent, '正在查询\n\n中间文本继续更新');
  stream.enqueue(encoder.encode('event: answer\ndata: {"answer":"数据库回答"}\n\n'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(messages.length, 3);
  assert.equal(messages[2].textContent, '数据库回答');
  assert.equal(p.nodes.get('#send').disabled, true);
  stream.enqueue(encoder.encode('event: done\ndata: {"answer":"数据库回答"}\n\n'));
  stream.close();
  await request;
  assert.equal(p.calls[1].url, 'http://localhost/chat/runs');
  assert.equal(p.calls[1].method, 'POST');
  assert.equal(p.calls[1].headers.Authorization, 'Bearer test-token');
  assert.equal('user_id' in JSON.parse(p.calls[1].body), false);
  assert.equal(p.calls[2].url, `http://localhost/chat/runs/${runId}/events`);
  assert.equal(p.calls[2].method, 'GET');
  assert.equal(p.calls[2].headers.Authorization, 'Bearer test-token');
  assert.equal(p.nodes.get('#runId').value, runId);
  assert.equal(messages.length, 3);
  const answer = messages[2];
  assert.equal(answer.children[0].textContent, 'Final Answer');
  assert.equal(answer.textContent, '数据库回答');
  assert.equal(p.nodes.get('#progress').textContent, 'Run complete');
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
  assert.match(p.nodes.get('#authStatus').textContent, /Incorrect username or password/);
  await p.login();
  p.run("elements.question.value = '问题'");
  await p.run('sendMessage()');
  assert.equal(p.nodes.get('#send').disabled, true);
  assert.match(p.nodes.get('#authStatus').textContent, /Please sign in again/);
});

test('cross-origin endpoint does not receive credentials', async () => {
  const p = page([]);
  p.run("elements.endpoint.value = 'https://example.com/chat'");
  await p.login();
  assert.equal(p.calls.length, 0);
  assert.match(p.nodes.get('#authStatus').textContent, /same origin/);
});
