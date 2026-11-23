// Exercise the shipped script and its event handlers with deterministic, reorderable responses.
// No browser, network, storage service or package dependencies.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const staticDir = path.join(__dirname, '../adaptive_rag/static');
const script = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));
const page = (items, has_more = false, next_offset = 0) => ({items, has_more, next_offset});
const row = id => ({conversation_id: id, title: id + ' title', updated_at: '2026-01-02T12:00:00Z'});
const turn = id => ({trace_id: id, question: 'question-' + id, status: 'evidence_preview', message: 'preview-' + id});
const trace = id => ({...turn(id), stages: [], final_result: {...turn(id), evidence: [
  {document_id: 'doc-' + id, evidence_id: 'source-' + id, text: 'evidence-' + id, images: [{key: 'pic', caption: 'figure-' + id}]}]}});
const sse = events => new Response(events.map(([name, data]) => 'event: ' + name + '\ndata: ' + JSON.stringify(data) + '\n\n').join(''),
  {headers: {'Content-Type': 'text/event-stream'}});

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.events = {}; this.value = ''; this.dataset = {}; }
  style = {};
  classList = {toggle() {}, remove() {}};
  get textContent() { return this.children.map(n => n.textContent ?? n).join(''); }
  set textContent(value) { this.children = [String(value)]; }
  get childElementCount() { return this.children.length; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this[key] = value; }
  addEventListener(type, fn) { this.events[type] = fn; }
  fire(type) { return this.events[type]({preventDefault() {}}); }
  focus() {} reset() {} showModal() { this.open = true; } close() { this.open = false; }
  buttons() { return this.children.flatMap(n => n.tag === 'button' ? [n] : n.buttons?.() || []); }
  tags(tag) { return this.children.flatMap(n => n.tag === tag ? [n, ...n.tags(tag)] : n.tags?.(tag) || []); }
}

const alice = {subject_id: 'alice', tenant_id: 'home'};
function signed(response, owner = alice) {
  response.headers.set('X-RAG-Principal', encodeURIComponent(owner.subject_id));
  response.headers.set('X-RAG-Tenant', encodeURIComponent(owner.tenant_id));
  return response;
}
async function workbench(route, bookmark = null, owner = alice, browser = {}) {
  const nodes = new Map([...html.matchAll(/id="([\w-]+)"/g)].map(m => ['#' + m[1], new Element()]));
  for (const key of ['.account-avatar', '#run-button span']) nodes.set(key, new Element());
  const get = key => { assert.ok(nodes.has(key), 'missing element ' + key); return nodes.get(key); };
  const document = {querySelector: get, querySelectorAll: sel => sel.includes('#conversation-list button') ?
    [...get('#conversation-list').buttons(), ...get('#chat-history').buttons()] : [],
    addEventListener() {}, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag), createTextNode: String};
  const history = {state: {ragConversation: bookmark}, replaceState(value) { this.state = value; }};
  const calls = [];
  const domains = {text: () => '', load: async () => {}, render() {}};
  const context = {document, history, window: {RagDomains: domains, addEventListener() {}, ...browser}, RagDomains: domains,
    IntersectionObserver: browser.IntersectionObserver || class { observe() {} disconnect() {} },
    Headers, crypto: require('node:crypto'), AbortController, TextDecoder, performance, setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame() {}, fetch: async (url, options) => {
      calls.push([url, options]);
      assert.equal(options.cache, 'no-store');
      if (url === '/console/session') return signed(Response.json(owner), owner);
      if (url === '/auth/logout') return signed(Response.json({}), owner);
      const result = await route(url, options);
      if (result instanceof Response && result.headers.has('X-RAG-Principal')) return result;
      return signed(result instanceof Response ? result : Response.json(result), owner);
    }};
  Object.assign(context, context.window);
  context.window = context;
  if (browser.useDomainControls) vm.runInNewContext(fs.readFileSync(path.join(staticDir, 'domain-controls.js'), 'utf8'), context);
  vm.runInNewContext(fs.readFileSync(path.join(staticDir, 'answer-visuals.js'), 'utf8'), context);
  vm.runInNewContext(script, context);
  await flush();
  const submit = async text => { get('#question').value = text; await get('#query-form').fire('submit'); await flush(); };
  return {get, calls, history, submit};
}

const sourceA = 'a'.repeat(64) + '.28.' + '1'.repeat(16);
const sourceB = 'b'.repeat(64) + '.41.' + '2'.repeat(16);
const excerpts = [sourceA, sourceB].map((evidence_id, i) => ({evidence_id, document_id: 'doc-' + i, text: 'excerpt-' + i}));
const citedResult = {trace_id: 'cited', generation: 'edition', status: 'answered', evidence: excerpts,
  answer: '第一项结论 [' + sourceB + ']\n第二项结论 [' + sourceA + ']', claims: [
    {text: '第一项结论', evidence_ids: [sourceB]}, {text: '第二项结论', evidence_ids: [sourceA]},
  ]};
async function citedWorkbench(result, restore = false) {
  const record = {...trace('cited'), final_result: result};
  return workbench(url => {
    if (url.startsWith('/conversations?')) return page([]);
    if (url === '/query/stream') return sse([['result', {result, trace: record}]]);
    if (url === '/conversations/group') return {conversation_id: 'group', turns: [{...turn('cited'), answer: result.answer}, turn('later')]};
    if (url === '/traces/later') return trace('later');
    if (url === '/traces/cited') return record;
    for (const item of excerpts) if (url === '/evidence/edition/' + item.evidence_id)
      return {...item, text: 'authorized original: ' + item.document_id};
    throw Error('Unexpected fixture request: ' + url);
  }, restore ? {subject: 'alice', tenant: 'home', id: 'group'} : null);
}

const legacyInsufficient = '找到了相关内容，但不足以完整支持这个问题，暂不生成答案；列出的是最接近的资料。';
const insufficientResult = {...citedResult, status: 'insufficient', message: legacyInsufficient,
  answer: null, claims: [], missing_query_ids: ['q1'], evidence: excerpts};

test('insufficient: current reply lists the returned passages and opens the selected original without an answer', async () => {
  const {get, submit, calls} = await citedWorkbench(insufficientResult);
  await submit('神经网络是如何工作的');
  const panel = get('#panel-answer');
  for (const item of excerpts) {
    assert.ok(panel.textContent.includes(item.document_id), 'document missing from reply');
    assert.ok(panel.textContent.includes(item.text), 'excerpt missing from reply');
  }
  assert.doesNotMatch(panel.textContent, /列出的是|q1/);
  assert.equal(get('#notice').hidden, true);
  await panel.buttons()[1].fire('click'); await flush();
  assert.equal(calls.at(-1)[0], '/evidence/edition/' + sourceB);
  assert.match(get('#source-content').textContent, /authorized original: doc-1/);
});

test('insufficient: compact history makes no list promise; selecting it loads only that turn and respects revoked source access', async () => {
  const {get, calls} = await workbench(url => {
    if (url.startsWith('/conversations?')) return page([]);
    if (url === '/conversations/group') return {conversation_id: 'group', turns: [
      {...turn('cited'), status: 'insufficient', answer: null, message: legacyInsufficient}, turn('later')]};
    if (url === '/traces/later') return trace('later');
    if (url === '/traces/cited') return {...trace('cited'), final_result: insufficientResult};
    if (url === '/evidence/edition/' + sourceA) return new Response('', {status: 404});
    throw Error('unexpected request ' + url);
  }, {subject: 'alice', tenant: 'home', id: 'group'});
  const history = get('#chat-history');
  assert.doesNotMatch(history.textContent, /列出的是|excerpt-0/);
  assert.ok(!calls.some(([url]) => url === '/traces/cited'));
  await history.buttons().find(b => b.textContent.includes('查看本轮资料')).fire('click');
  assert.match(history.textContent, /doc-0.*excerpt-0.*doc-1.*excerpt-1/s);
  assert.equal(get('#notice').hidden, true);
  await history.buttons().find(b => b.textContent.includes('查看原文')).fire('click'); await flush();
  assert.equal(calls.filter(([url]) => url === '/query/stream').length, 0);
  assert.doesNotMatch(history.textContent + get('#source-content').textContent, /excerpt-|doc-0/);
});

test('insufficient: an older result without final evidence does not pretend to have references', async () => {
  const {get, submit} = await citedWorkbench({...insufficientResult, evidence: []});
  await submit('神经网络是如何工作的');
  const panel = get('#panel-answer');
  assert.match(panel.textContent, /没有可展示的.*资料/);
  assert.doesNotMatch(panel.textContent, /找到了相关内容|列出的是|最接近|q1/);
  assert.equal(panel.buttons().length, 0);
});

test('claims appear once in answer order and each inline source opens its own authorized original', async () => {
  const {get, submit, calls} = await citedWorkbench(citedResult);
  await submit('两项结论是什么？');
  const panel = get('#panel-answer');
  for (const claim of citedResult.claims) assert.equal(panel.textContent.split(claim.text).length - 1, 1, claim.text);
  for (const id of [sourceA, sourceB]) assert.ok(!panel.textContent.includes(id));
  assert.match(panel.textContent, /第一项结论\s*来源\s*2.*第二项结论\s*来源\s*1/s);
  assert.equal(panel.buttons().length, 2);
  for (const [index, expected] of [1, 0].entries()) {
    await panel.buttons()[index].fire('click'); await flush();
    assert.equal(get('#source-dialog').open, true);
    assert.equal(calls.at(-1)[0], '/evidence/edition/' + excerpts[expected].evidence_id);
    assert.ok(get('#source-content').textContent.includes('authorized original: doc-' + expected));
    get('#source-dialog').close();
  }
});

test('formats claims safely and charts only complete cited source data', async () => {
  const table = '| 日期 | 数量 |\n|---|---|\n|2026-01|12|\n|2026-02|18|\n|2026-04|25|';
  const result = {...citedResult, claims: [{text: '## 摘要\n\n- **增长**\n- <img src=x onerror=alert(1)>', evidence_ids: [sourceA]}],
    evidence: [{...excerpts[0], text: table}, {...excerpts[1], text: table.replace('数量', '未引用数据')}]};
  const {get, submit} = await citedWorkbench(result);
  await submit('数量变化？');
  const panel = get('#panel-answer');
  assert.equal(panel.tags('h4')[0].textContent, '摘要');
  assert.equal(panel.tags('strong')[0].textContent, '增长');
  assert.equal(panel.tags('img').length, 0);
  assert.ok(panel.textContent.includes('<img src=x onerror=alert(1)>'));
  assert.equal(panel.tags('svg').length, 1);
  assert.equal(panel.tags('td').at(-1).textContent, '25');
  assert.ok(!panel.textContent.includes('未引用数据'));
  assert.equal(panel.tags('figure')[0].buttons()[0].textContent, '来源 1');
});

async function previewCharts(text, {status = 'evidence_preview', truncated = false, locator} = {}) {
  const result = {...citedResult, status, answer: null, claims: [], evidence: [{...excerpts[0], text, truncated, locator}]};
  const {get, submit} = await citedWorkbench(result);
  await submit('看原表');
  return get('#panel-answer').tags('figure');
}
test('signed bars use a common zero baseline and retain the exact source values', async () => {
  const [fig] = await previewCharts('| 地区 | 净增长 |\n|---|---|\n|北|-10|\n|东|0|\n|南|30|');
  const tracks = fig.tags('div').filter(n => n.className === 'answer-bar-track');
  assert.deepEqual(tracks.map(t => t.children.map(c => c.style.insetInlineStart)), [['0%', '25%'], ['25%', '25%'], ['25%', '25%']]);
  assert.deepEqual(tracks.map(t => t.children[0].style.inlineSize), ['25%', '0%', '75%']);
  assert.deepEqual(fig.tags('strong').map(n => n.textContent), ['-10', '0', '30']);
});
test('wide calendar table: two separate measures, time-scaled x axis, unmodified source order', async () => {
  const figures = await previewCharts('|月份|出货量|准时率|\n|---|---|---|\n|2026-04|34|80%|\n|2026-01|11|75%|\n|2026-02|12|70%|');
  assert.deepEqual(figures.map(f => f.tags('figcaption')[0].textContent), ['出货量 · 趋势', '准时率 · 趋势']);
  for (const f of figures) {
    const xs = f.tags('circle').map(n => Number(n.cx));
    assert.ok(Math.abs((xs[1] - xs[0]) / (xs[2] - xs[0]) - 31 / 90) < .00001);
    assert.deepEqual(f.tags('td').slice(0, 3).map(n => n.textContent), ['2026-04', '34', '80%']);
  }
});
test('flow requires consecutive declared steps, never infers them from prose', async () => {
  const table = '|步骤|操作|\n|---|---|\n|1|核对资料|\n|2|提交申请|\n|3|复核签字|';
  const [flow] = await previewCharts(table);
  assert.deepEqual(flow.tags('li').map(n => n.textContent), ['核对资料', '提交申请', '复核签字']);
  assert.equal((await previewCharts(table.replace('|2|', '|4|'))).length, 0);
});
test('does not visualize mixed units, identifier columns, malformed, fenced or insufficient evidence', async () => {
  const t = '|地区|指标|\n|---|---|\n|北|10|\n|东|20|\n|南|30|';
  for (const bad of [t.replace('10', '10%'), t.replace('指标', '产品编号'), t.replace('20', '020'),
      t + '\n|西|40', t + '\n|其\\|他|40|', '```md\n' + t + '\n```']) {
    assert.equal((await previewCharts(bad)).length, 0, bad);
  }
  for (const flags of [{truncated: true}, {locator: 'lines:2-21;structure_fragment:table;chars:0-128'},
      {status: 'insufficient'}, {status: 'empty'}])
    assert.equal((await previewCharts(t, flags)).length, 0);
});
test('quarters with explicit years form a timeline, ambiguous periods do not', async () => {
  const t = '|季度|营收|\n|---|---|\n|2026年第一季度|10|\n|2026-Q2|20|\n|2027Q1|30|';
  assert.equal((await previewCharts(t))[0].tags('svg').length, 1);
  assert.equal((await previewCharts(t.replace('2027Q1', '第一季度'))).length, 0);
});

test('personal retrieval settings persist separately by tenant and stable subject', async () => {
  const storage = new Map(), requests = [];
  const browser = {localStorage: {getItem: key => storage.get(key), setItem: (key, value) => storage.set(key, value)}};
  const route = (url, options) => {
    if (url.startsWith('/conversations?')) return page([]);
    assert.equal(url, '/query/stream'); requests.push(JSON.parse(options.body));
    return sse([['result', {result: trace('run').final_result, trace: trace('run') }]]);
  };
  const owner = {...alice, capabilities: {plugins: {rewrite: {enabled: true}, hyde: {enabled: false}}}};
  const first = await workbench(route, null, owner, browser);
  assert.equal(first.get('#query-hyde').disabled, true);
  first.get('#top-k').value = '10'; first.get('#top-k').fire('change');
  first.get('#sku').value = 'sku-1'; first.get('#sku').fire('change');
  first.get('#query-rewrite').checked = false; first.get('#query-rewrite').fire('change');
  for (const other of [{...owner, subject_id: 'bob'}, {...owner, tenant_id: 'other'}]) {
    const tab = await workbench(route, null, other, browser);
    await tab.submit('unaffected');
    assert.equal(requests.at(-1).top_k, 5);
    assert.equal(tab.get('#sku').value, '');
    assert.equal(requests.at(-1).skip_query_steps, undefined);
  }
  const restored = await workbench(route, null, owner, browser);
  restored.get('#new-chat').fire('click');
  await restored.submit('personal');
  assert.equal(requests.at(-1).top_k, 10);
  assert.equal(requests.at(-1).sku, 'sku-1');
  assert.deepEqual(requests.at(-1).skip_query_steps, ['rewrite']);
  restored.get('#query-options-reset').fire('click');
  await restored.submit('reset');
  assert.equal(requests.at(-1).top_k, 5);
  assert.equal(requests.at(-1).skip_query_steps, undefined);
});

test('legacy answers convert only complete line-end evidence tokens, preserving literal brackets and math', async () => {
  const literal = '普通 [说明]、[12]、区间 [0, 1] 与 f(x) = A[i] + B[j]。';
  const embedded = '标识 [' + sourceB + '] 在句中不是出处。';
  const malformed = '[' + sourceA.slice(0, -1) + ']';
  const {get, submit} = await citedWorkbench({...citedResult, claims: [], answer:
    [literal, embedded, malformed, '第一段。 [' + sourceA + ']', '第二段。 [' + sourceB + '] [' + sourceA + ']'].join('\r\n')});
  await submit('旧回答');
  const panel = get('#panel-answer');
  for (const text of [literal, embedded, malformed]) assert.ok(panel.textContent.includes(text), text);
  assert.ok(!panel.textContent.includes('[' + sourceA + ']'), 'line-end hash still visible');
  assert.deepEqual(panel.buttons().map(b => b.textContent.match(/来源\s*(\d+)/)?.[1]), ['1', '2', '1']);
});

test('restored history hides citation hashes and the selected turn opens its own inline source', async () => {
  const {get, calls} = await citedWorkbench(citedResult, true);
  const history = get('#chat-history');
  assert.match(history.textContent, /第一项结论/);
  for (const id of [sourceA, sourceB]) assert.ok(!history.textContent.includes(id), 'history exposes citation ID');
  await history.buttons().find(b => b.textContent.includes('查看本轮流程')).fire('click');
  assert.ok(calls.some(([url]) => url === '/traces/cited'));
  const source = history.buttons().find(b => b.textContent === '来源 2');
  assert.ok(source, 'selected historical answer has an inline source');
  await source.fire('click'); await flush();
  assert.equal(get('#source-dialog').open, true);
  assert.match(get('#source-content').textContent, /authorized original: doc-1/);
});

test('a filtered page advances by next_offset; restore and inspect without moving any turn', async () => {
  const {get, calls} = await workbench(url => {
    if (url.endsWith('offset=0')) return page([], true, 20);
    if (url.endsWith('offset=20')) return page([row('a')]);
    if (url === '/conversations/a') return {conversation_id: 'a', turns: [turn('one'), turn('two')]};
    if (url.startsWith('/traces/')) return trace(url.slice(8));
    throw Error(url);
  });
  assert.equal(get('#conversation-more').hidden, false);
  get('#conversation-more').fire('click'); await flush();
  assert.equal(get('#conversation-more').hidden, true);
  await get('#conversation-list').buttons()[0].fire('click');
  assert.deepEqual(calls.map(([url]) => url).slice(1), ['/conversations?limit=20&offset=0', '/conversations?limit=20&offset=20', '/conversations/a', '/traces/two']);
  assert.equal(get('#export-button').disabled, false);
  await get('#chat-history').buttons()[0].fire('click');
  const chat = get('#chat-history').textContent;
  assert.deepEqual(chat.match(/question-(one|two)/g), ['question-one', 'question-two']);
  assert.match(chat, /preview-one.*question-two.*preview-two/);
  assert.equal(get('#current-chat').hidden, true);
  assert.match(get('#panel-evidence').textContent, /figure-one/);
  assert.doesNotMatch(get('#raw-data').textContent, /question-two/);
});

test('new and continued streams send no client history; result-only stream can establish a conversation', async () => {
  const bodies = [];
  const {get, history, submit} = await workbench((url, options) => {
    if (url.startsWith('/conversations?')) return page([row('saved')]);
    assert.equal(url, '/query/stream');
    bodies.push(JSON.parse(options.body));
    const id = 'id' + bodies.length;
    const events = bodies.length === 3 ? [] : [['trace', {trace_id: id}]];
    return sse([...events, ['result', {trace: trace(id), result: trace(id).final_result}]]);
  });
  await submit('one'); await submit('two');
  assert.equal(history.state.ragConversation.id, 'id1');
  get('#new-chat').fire('click');
  assert.equal(history.state.ragConversation, null);
  assert.equal(get('#chat-history').textContent, '');
  assert.match(get('#conversation-list').textContent, /saved title/);
  await submit('three');
  assert.equal(history.state.ragConversation.id, 'id3');
  assert.deepEqual(bodies.map(b => b.conversation_id), [undefined, 'id1', undefined]);
  assert.ok(bodies.every(b => b.persist_conversation === true && !('history' in b)));
});

for (const denial of [403, 404, 'not_found', 'forbidden', 'recovery']) {
  test('revoked continuation clears all previously readable output: ' + denial, async () => {
    let denied = false;
    const {get, calls, history, submit} = await workbench(url => {
      if (url.includes('?')) return page(denied ? [] : [row('private')]);
      if (url === '/conversations/private') return {conversation_id: 'private', turns: [turn('private')]};
      if (url === '/traces/private') return trace('private');
      if (url === '/traces/interrupted') return new Response(null, {status: 404});
      assert.equal(url, '/query/stream'); denied = true;
      if (denial === 'recovery') return sse([['trace', {trace_id: 'interrupted'}]]);
      return typeof denial === 'number' ? new Response(null, {status: denial}) :
        sse([['error', {code: denial, trace_id: 'forbidden', trace: trace('forbidden') }]]);
    }, {subject: 'alice', tenant: 'home', id: 'private'});
    assert.match(get('#chat-history').textContent, /private/);
    assert.match(get('#raw-data').textContent, /private/);
    await submit('next');
    assert.equal(history.state.ragConversation, null);
    for (const id of ['chat-history', 'conversation-list', 'panel-answer', 'panel-evidence', 'raw-data', 'source-content', 'timeline', 'inspector-body'])
      assert.doesNotMatch(get('#' + id).textContent, /private|forbidden/);
    assert.equal(get('#export-button').disabled, true);
    assert.equal(calls.filter(([url]) => url === '/query/stream').length, 1);
  });
}

for (const [transport, revokedAt] of [['SSE', null], ['SSE', 'group'], ['SSE', 'latest'], ['HTTP', null]]) {
  test(transport + ' conversation_full rechecks the saved group, not the rejected trace: ' + (revokedAt || 'readable'), async () => {
    const committed = Array.from({length: 63}, (_, i) => turn('private-' + (i + 1)));
    let rejected = false, finishDetail;
    const {get, history, calls, submit} = await workbench(url => {
      if (url.includes('?')) return page(rejected && revokedAt ? [] : [row('group')]);
      if (url === '/conversations/group') return rejected ? new Promise(resolve => finishDetail = resolve) :
        {conversation_id: 'group', turns: committed};
      if (url === '/traces/private-63') return trace('private-63');
      if (url === '/traces/winner') return revokedAt === 'latest' ? new Response(null, {status: 404}) : trace('winner');
      if (url === '/traces/loser') return new Response(null, {status: 404});
      assert.equal(url, '/query/stream');
      rejected = true;
      if (transport === 'HTTP') return Response.json({error: {code: 'conversation_full'}}, {status: 409});
      return sse([['trace', {trace_id: 'loser'}], ['error', {code: 'conversation_full', trace_id: 'loser'}]]);
    }, {subject: 'alice', tenant: 'home', id: 'group'});
    assert.match(get('#raw-data').textContent, /private-63/);
    const submission = submit('rejected question'); await flush();
    assert.ok(!calls.some(([url]) => url === '/traces/loser'));
    assert.equal(get('#chat-history').textContent, '');
    assert.equal(get('#export-button').disabled, true);
    finishDetail(revokedAt === 'group' ? new Response(null, {status: 403}) :
      {conversation_id: 'group', turns: [...committed, turn('winner')]});
    await submission;
    assert.equal(calls.filter(([url]) => url === '/query/stream').length, 1);
    if (revokedAt) {
      assert.equal(history.state.ragConversation, null);
      for (const id of ['chat-history', 'conversation-list', 'chat-question', 'raw-data', 'panel-evidence', 'notice'])
        assert.doesNotMatch(get('#' + id).textContent, /private-|winner|loser|rejected question|轮数上限/);
      assert.equal(get('#export-button').disabled, true);
    } else {
      assert.equal(history.state.ragConversation.id, 'group');
      assert.equal(get('#chat-history').buttons().length, 64);
      assert.match(get('#raw-data').textContent, /question-winner/);
      assert.doesNotMatch(get('#chat-history').textContent, /loser|rejected question/);
      assert.match(get('#notice').textContent, /轮数上限.*新建对话/);
    }
  });
}

test('selecting an inaccessible conversation discards the previous view before detail returns', async () => {
  let finish;
  const {get, history} = await workbench(url => {
    if (url.includes('?')) return page([row('first'), row('removed')]);
    if (url === '/conversations/first') return {conversation_id: 'first', turns: [turn('first')]};
    if (url === '/traces/first') return trace('first');
    assert.equal(url, '/conversations/removed');
    return new Promise(resolve => finish = resolve);
  });
  await get('#conversation-list').buttons()[0].fire('click');
  const switching = get('#conversation-list').buttons()[1].fire('click');
  assert.equal(get('#run-button').disabled, true);
  assert.equal(get('#chat-history').textContent, '');
  assert.doesNotMatch(get('#raw-data').textContent, /question-first/);
  finish(new Response(null, {status: 404})); await switching;
  assert.equal(get('#run-button').disabled, false);
  assert.equal(get('#export-button').disabled, true);
  assert.equal(history.state.ragConversation, null);
});

test('requests carry the displayed owner, even for logout after the screen is cleared', async () => {
  const owner = {subject_id: '张 /+%', tenant_id: '组/A'};
  const {calls, get, submit} = await workbench(url => url.startsWith('/conversations?') ? page([]) :
    sse([['result', {trace: trace('t'), result: trace('t').final_result}]]), null, owner);
  await submit('hello');
  await get('#logout-button').fire('click');
  for (const [url, options] of calls) {
    const headers = new Headers(options.headers);
    assert.equal(headers.get('X-RAG-Principal'), url === '/console/session' ? null : '%E5%BC%A0%20%2F%2B%25', url);
    assert.equal(headers.get('X-RAG-Tenant'), url === '/console/session' ? null : '%E7%BB%84%2FA', url);
  }
});

for (const [status, owner] of [[409, {...alice, tenant_id: 'away'}], [200, {...alice, subject_id: 'bob'}]]) {
  test('foreign authenticated response clears the tab without consuming its body: ' + status, async () => {
    let foreign, writes = 0;
    const {get, history, submit} = await workbench(url => {
      if (url.includes('?')) return page([row('private')]);
      if (url === '/conversations/private') return {conversation_id: 'private', turns: [turn('private')]};
      if (url === '/traces/private') return trace('private');
      writes++;
      foreign = signed(status === 200 ? sse([['result', {trace: trace('foreign')}]]) : Response.json({detail: 'foreign'}, {status}), owner);
      return foreign;
    }, {subject: 'alice', tenant: 'home', id: 'private'});
    assert.match(get('#raw-data').textContent, /private/);
    await submit('continue');
    assert.equal(writes, 1);
    assert.equal(foreign.bodyUsed, false);
    assert.equal(history.state.ragConversation, null);
    assert.equal(get('#app-shell').hidden, true);
    for (const id of ['chat-history', 'conversation-list', 'raw-data', 'notice', 'login-error'])
      assert.doesNotMatch(get('#' + id).textContent, /private|foreign/);
  });
}

test('a same-subject bookmark from another tenant is never requested', async () => {
  const {calls, history} = await workbench(() => page([]), {subject: 'alice', tenant: 'away', id: 'other'});
  assert.ok(!calls.some(([url]) => url === '/conversations/other'));
  assert.equal(history.state.ragConversation, null);
});

test('the shared domain catalog also requests and checks the displayed identity', async () => {
  let headers, foreign;
  const {get} = await workbench((url, options) => {
    if (url !== '/access/domains') return page([]);
    headers = new Headers(options.headers);
    foreign = signed(Response.json({domains: []}), {...alice, subject_id: 'other'});
    return foreign;
  }, null, alice, {useDomainControls: true});
  assert.equal(headers.get('X-RAG-Principal'), 'alice');
  assert.equal(foreign.bodyUsed, false);
  assert.equal(get('#app-shell').hidden, true);
});

test('evidence thumbnails cannot fetch or display bytes under another tenant', async () => {
  let imageHeaders, foreign;
  class Visible { constructor(fn) { this.fn = fn; } observe(target) { queueMicrotask(() => this.fn([{isIntersecting: true, target}], this)); } unobserve() {} disconnect() {} }
  const {get} = await workbench((url, options) => {
    if (url.includes('?')) return page([row('picture')]);
    if (url === '/conversations/picture') return {conversation_id: 'picture', turns: [turn('picture')]};
    if (url === '/traces/picture') return trace('picture');
    assert.ok(url.endsWith('/images/pic'));
    imageHeaders = new Headers(options.headers);
    foreign = signed(new Response('image bytes'), {...alice, tenant_id: 'other'});
    return foreign;
  }, {subject: 'alice', tenant: 'home', id: 'picture'}, alice, {IntersectionObserver: Visible});
  assert.equal(imageHeaders?.get('X-RAG-Tenant'), 'home');
  assert.equal(foreign.bodyUsed, false);
  assert.equal(get('#app-shell').hidden, true);
});

test('logout in another tab clears already visible private data without a new query', async () => {
  const listeners = [];
  const browser = {localStorage: {setItem(key) { for (const fn of listeners) fn({key}); }}};
  const route = url => url.includes('?') ? page([row('private')]) :
    url.startsWith('/traces/') ? trace('private') : {conversation_id: 'private', turns: [turn('private')]};
  const first = await workbench(route, null, alice, browser);
  const other = await workbench(route, {subject: 'alice', tenant: 'home', id: 'private'}, alice,
    {...browser, addEventListener(name, fn) { if (name === 'storage') listeners.push(fn); }});
  assert.match(other.get('#raw-data').textContent, /private/);
  await first.get('#logout-button').fire('click');
  assert.equal(other.get('#app-shell').hidden, true);
  assert.equal(other.history.state.ragConversation, null);
  assert.doesNotMatch(other.get('#raw-data').textContent, /private/);
});

test('detail 503 leaves a retryable selection, not a sendable blank chat', async () => {
  let attempts = 0;
  const {get, history, calls, submit} = await workbench(url => {
    if (url.includes('?')) return page([]);
    if (url === '/traces/again') return trace('again');
    assert.equal(url, '/conversations/again');
    return ++attempts === 1 ? new Response(null, {status: 503}) : {conversation_id: 'again', turns: [turn('again')]};
  }, {subject: 'alice', tenant: 'home', id: 'again'});
  assert.equal(get('#run-button').disabled, true);
  assert.equal(history.state.ragConversation.id, 'again');
  await submit('should not start a query');
  assert.ok(!calls.some(([url]) => url === '/query/stream'));
  await get('#notice').buttons()[0].fire('click');
  assert.equal(get('#run-button').disabled, false);
  assert.match(get('#raw-data').textContent, /question-again/);
});

test('an interrupted opening trace is not a bookmark or a continuation target', async () => {
  let finishRecovery;
  const bodies = [];
  const {get, history, submit} = await workbench((url, options) => {
    if (url.includes('?')) return page([]);
    if (url === '/traces/early') return new Promise(resolve => finishRecovery = resolve);
    bodies.push(JSON.parse(options.body));
    return bodies.length === 1 ? sse([['trace', {trace_id: 'early', conversation_id: 'early'}]]) :
      sse([['result', {trace: trace('done'), result: trace('done').final_result}]]);
  });
  const interrupted = submit('unpersisted'); await flush();
  try { assert.equal(history.state.ragConversation, null); }
  finally { finishRecovery(new Response(null, {status: 503})); await interrupted; }
  assert.equal(history.state.ragConversation, null);
  await submit('fresh');
  assert.equal(bodies[1].conversation_id, undefined);
  assert.doesNotMatch(get('#chat-history').textContent, /unpersisted/);
  assert.equal(history.state.ragConversation.id, 'done');
});

test('a readable recovered trace establishes its persisted conversation, not the early SSE ID', async () => {
  const {calls, history, submit} = await workbench(url => {
    if (url.includes('?')) return page([]);
    if (url === '/traces/run') return {...trace('run'), conversation_id: 'group'};
    return sse([['trace', {trace_id: 'run', conversation_id: 'provisional'}]]);
  });
  await submit('recover');
  assert.equal(history.state.ragConversation.id, 'group');
  assert.equal(calls.filter(([url]) => url === '/query/stream').length, 1);
});

test('trace detail 503 neither erases the readable transcript nor presents a running query', async () => {
  let attempt = 0;
  const {get, calls} = await workbench(url => {
    if (url.includes('?')) return page([]);
    if (url === '/conversations/group') return {conversation_id: 'group', turns: [turn('one')]};
    return ++attempt === 1 ? new Response(null, {status: 503}) : trace('one');
  }, {subject: 'alice', tenant: 'home', id: 'group'});
  assert.match(get('#chat-history').textContent, /question-one/);
  assert.doesNotMatch(get('#panel-evidence').textContent, /查询执行中/);
  assert.equal(get('#export-button').disabled, true);
  await get('#notice').buttons()[0].fire('click');
  assert.match(get('#raw-data').textContent, /question-one/);
  assert.ok(!calls.some(([url]) => url === '/query/stream'));
});

test('late detail and trace cannot replace a new chat; late list cannot survive logout', async () => {
  const pending = {};
  const {get, history, calls} = await workbench(url => {
    if (url.endsWith('offset=0')) return page([row('slow'), row('recent')], true, 35);
    if (url === '/conversations/recent') return {conversation_id: 'recent', turns: [turn('recent')]};
    return new Promise(resolve => pending[url] = resolve);
  }, {subject: 'bob', id: 'other-user'});
  assert.ok(!calls.some(([url]) => url.includes('other-user')));
  get('#conversation-list').buttons()[0].fire('click');
  get('#conversation-list').buttons()[1].fire('click'); await flush();
  assert.match(get('#chat-history').textContent, /recent/);
  get('#new-chat').fire('click');
  pending['/conversations/slow']({conversation_id: 'slow', turns: [turn('slow')]});
  pending['/traces/recent'](trace('recent')); await flush();
  assert.equal(get('#chat-history').textContent, '');
  assert.doesNotMatch(get('#raw-data').textContent, /recent|slow/);
  assert.equal(history.state.ragConversation, null);
  get('#conversation-more').fire('click');
  await get('#logout-button').fire('click');
  pending['/conversations?limit=20&offset=35'](page([row('late')])); await flush();
  assert.equal(get('#conversation-list').textContent, '');
  assert.equal(get('#app-shell').hidden, true);
});

test('preflight inspector distinguishes a suggested transform from actual execution and explains fallback', async () => {
  const outputs = [
    [{route: 'kb', source: 'deepseek', selected_steps: ['rewrite']}, /DeepSeek 意图判断：检索知识库/, /建议：改写.*个人设置/],
    [{route: 'exact', source: 'rule_fallback', selected_steps: [], fallback_reason: 'cloud_timeout'}, /已回退到规则：按标识符查资料/, /回退原因：等待意图服务超时/],
  ];
  for (const [output, routeLabel, detail] of outputs) {
    const record = {...trace('cited'), stages: [{index: 0, name: 'decision.preflight', status: 'ok', duration_ms: 1,
      output: {...output, checkpoint: 'preflight'}}], final_result: citedResult};
    const wb = await workbench(url => {
      if (url.startsWith('/conversations?')) return page([]);
      if (url === '/query/stream') return sse([['result', {result: citedResult, trace: record}]]);
      throw Error('unexpected ' + url);
    });
    await wb.submit('保修');
    const button = wb.get('#timeline').buttons()[0];
    assert.ok(button);
    button.fire('click');
    const description = wb.get('#inspector-body').textContent;
    assert.match(description, routeLabel);
    assert.match(description, detail);
  }
});
