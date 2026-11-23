const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const load = (ctx, name) => vm.runInContext(fs.readFileSync(path.join(__dirname, '../adaptive_rag/static', name), 'utf8'), ctx);
const alice = {subject_id: '读者', tenant_id: 'north', username: 'Reader', capabilities: {mode: 'model_free'}};
const bob = {...alice, tenant_id: 'south'};
const tags = user => ({'X-RAG-Principal': encodeURIComponent(user.subject_id), 'X-RAG-Tenant': user.tenant_id});
const json = (value, user = alice) => Response.json(value, {headers: tags(user)});
const flush = () => new Promise(resolve => setImmediate(resolve));

function fixture(route, session = json(alice)) {
  const calls = [], events = {}, nodes = new Map();
  const node = () => ({children: [], events: {}, dataset: {}, classList: {toggle() {}},
    append(...items) { this.children.push(...items); }, replaceChildren() { this.children = []; },
    setAttribute() {}, addEventListener(e, cb) { this.events[e] = cb; }, showModal() { this.open = true; }});
  const get = id => { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); };
  let observe;
  const ctx = vm.createContext({Headers, AbortController,
    document: {documentElement: {}, getElementById: get, createElement: node},
    addEventListener(e, cb) { events[e] = cb; }, location: {replace(url) { this.redirect = url; }},
    IntersectionObserver: class { constructor(cb) { observe = cb; } observe() {} unobserve() {} disconnect() {} },
    fetch: async (url, options) => {
      calls.push(url);
      if (url === '/console/session') return session;
      assert.equal(options.mode, 'same-origin');
      assert.equal(options.cache, 'no-store');
      assert.equal(options.redirect, 'error');
      for (const [k, v] of Object.entries(tags(alice))) assert.equal(options.headers.get(k), v, url);
      return route(url, options);
    }});
  ctx.window = ctx;
  load(ctx, 'page-owner.js');
  return {ctx, calls, events, get, enter: () => vm.runInContext('pageOwner.session()', ctx),
    thumbnail: target => observe([{target, isIntersecting: true}]), eval: expr => vm.runInContext(expr, ctx)};
}

test('file writes carry the displayed owner even when the cookie switches tenant; mismatch permanently closes the page', async () => {
  let cookie = alice, writes = 0;
  const f = fixture((url, options) => {
    assert.equal(url, '/files/documents');
    if (options.headers.get('X-RAG-Tenant') !== cookie.tenant_id)
      return new Response('private error', {status: 409, headers: tags(cookie)});
    writes++;
    return json({saved: true});
  });
  load(f.ctx, 'file-center.js');
  const save = () => f.eval('fileCenter.json("/files/documents", "POST", {content:"draft"})');
  await assert.rejects(save());
  await f.enter(); await save();
  cookie = bob;
  await assert.rejects(save(), /身份已变更/);
  await assert.rejects(save());
  assert.equal(writes, 1);
  assert.deepEqual(f.calls, ['/console/session', '/files/documents', '/files/documents']);
  assert.equal(f.ctx.location.redirect, '/');
  assert.equal(f.ctx.document.documentElement.hidden, true);
});

test('image page binds catalog, assets and index action and never consumes a wrong-owner thumbnail', async () => {
  const media = new Response('private pixels', {headers: {...tags(bob), 'Content-Type': 'image/jpeg'}});
  const f = fixture(url => {
    if (url === '/access/domains') return json({domains: []});
    if (url === '/assets') return json({assets: [{asset_id: 'pic', title: 'Pic'}]});
    if (url === '/assets/index') return json({processed: 0});
    assert.equal(url, '/assets/pic/thumbnail');
    return media;
  });
  load(f.ctx, 'domain-controls.js'); load(f.ctx, 'images.js'); await flush();
  await f.get('image-index').events.click();
  const img = f.get('image-results').children[0].children[0].children[0];
  assert.equal(img.src, undefined);
  img.isConnected = true; f.thumbnail(img); await flush();
  assert.deepEqual(f.calls, ['/console/session', '/access/domains', '/assets', '/assets/index', '/assets', '/assets/pic/thumbnail']);
  assert.equal(media.bodyUsed, false);
  assert.equal(img.src, undefined);
  assert.equal(f.ctx.location.redirect, '/');
});

test('original image action refuses unsigned bytes before opening them', async () => {
  const media = new Response('private pixels');
  const f = fixture(url => {
    if (url === '/access/domains') return json({domains: []});
    if (url === '/assets') return json({assets: [{asset_id: 'pic'}]});
    assert.equal(url, '/assets/pic/content'); return media;
  });
  load(f.ctx, 'domain-controls.js'); load(f.ctx, 'images.js'); await flush();
  await f.get('image-results').children[0].children[0].events.click();
  assert.equal(media.bodyUsed, false);
  assert.equal(f.get('image-original').open, undefined);
  assert.equal(f.ctx.location.redirect, '/');
});

test('bootstrap cannot bind a mismatched owner and a late response cannot survive a session-change notification', async () => {
  const bad = fixture(() => assert.fail(), json(alice, bob));
  await assert.rejects(bad.enter());
  assert.equal(bad.ctx.location.redirect, '/');
  let finish;
  const f = fixture(() => new Promise(resolve => { finish = resolve; }));
  await f.enter();
  const pending = f.eval('pageOwner.request("/assets")');
  f.events.storage({key: 'rag.session.changed'});
  const response = json({private: true});
  finish(response);
  await assert.rejects(pending);
  assert.equal(response.bodyUsed, false);
});
