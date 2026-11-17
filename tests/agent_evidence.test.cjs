const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Exercise the function shipped in the page rather than a test-side copy.
const html = fs.readFileSync(path.join(__dirname, "../procureflow/web/index.html"), "utf8");
const begin = html.indexOf("    function evidenceNotice(trace) {");
const end = html.indexOf("    function renderTrace(trace) {", begin);
assert.ok(begin >= 0 && end > begin, "evidence notice function is available");
const evidenceNotice = new Function(html.slice(begin, end) + "\nreturn evidenceNotice;")();

test("no successful business query is made visible", () => {
  for (const trace of [{events: []}, {events: [{tool: "list_stock", outcome: "rejected"}]}]) {
    assert.match(evidenceNotice(trace), /没有成功查询业务数据.*无法提供可靠的业务数据/);
  }
});

test("a successful query never certifies the model answer", () => {
  for (const tool of ["list_stock", "list_quotes", "list_recommendations"]) {
    const notice = evidenceNotice({events: [{tool, outcome: "ok"}]});
    assert.match(notice, /已查询业务数据.*系统中的库存、报价和采购预览/);
    assert.doesNotMatch(notice, /已核实|全部正确|均准确|所有结论|已验证/);
  }
});

test("missing, malformed, or unrecognized successful traces are unknown", () => {
  for (const trace of [null, {}, {events: [null]}, {events: [{tool: "future_tool", outcome: "ok"}]}]) {
    assert.match(evidenceNotice(trace), /无法确认本次查询依据/);
  }
});

test("notice is outside the collapsed trace and refreshed with each answer", () => {
  const notice = html.indexOf('id="agent-evidence"');
  const answer = html.indexOf('id="agent-answer"');
  const trace = html.indexOf("<details><summary>查看工具调用记录");
  assert.ok(notice >= 0 && notice < answer && answer < trace);
  assert.match(html, /element\("agent-evidence"\)\.textContent = evidenceNotice\(result\.trace\)/);
});

function snippet(start, end) {
  const left = html.indexOf(start), right = html.indexOf(end, left);
  assert.ok(left >= 0 && right > left);
  return html.slice(left, right);
}

function agentHarness() {
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, {
      value: "", hidden: false, textContent: "", attributes: {}, listeners: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      replaceChildren() { this.textContent = ""; }, focus() {},
    });
    return nodes.get(id);
  };
  const waiting = [];
  const session = name => ({identity: {actor_id: name}, token: "example-token", busy: false,
    controller: {abort() { this.aborted = true; }}});
  const context = vm.createContext({element, active: session("buyer-a"), modelConfigured: true,
    asOf: "2026-01-01", horizon: 7, legacyAcknowledged: false,
    controls() {}, notice() {}, clearPreview() {}, optionalModel() {},
    evidenceNotice() { return ""; }, renderSystemRules() {}, renderTrace() {},
    async loadDrafts() {}, async pendingRequest() { return "request-id"; },
    request() { return new Promise((resolve, reject) => waiting.push({resolve, reject})); },
  });
  vm.runInContext([
    snippet("    function agentProgress(session, working) {", "    function amount(value) {"),
    snippet("    function clearSession(message, error = false) {", "    async function request(path, session,"),
    snippet("    async function interact(working, handler,", "    function renderStock(stock, recommendations) {"),
    snippet('    element("agent-form").addEventListener("submit",', '    request("/healthz", null)'),
  ].join("\n"), context);
  element("prompt").value = "查询库存";
  return {context, element, waiting, session,
    submit() { element("agent-form").listeners.submit({preventDefault() {}}); }};
}

const flush = () => new Promise(resolve => setImmediate(resolve));
const answer = {answer: "库存结果", trace: {events: []}, authoritative_rules: [], draft_id: null};

test("agent progress is accessible and clears after success or failure", async () => {
  assert.match(html, /id="agent-progress"[^>]*role="status"[^>]*aria-live="polite"[^>]*hidden/);
  const h = agentHarness();
  h.submit();
  assert.equal(h.element("agent-progress").hidden, false);
  assert.match(h.element("agent-progress").textContent, /正在查询/);
  assert.equal(h.element("agent-form").attributes["aria-busy"], "true");
  await flush();
  h.waiting.shift().resolve(answer);
  await flush();
  assert.equal(h.element("agent-progress").hidden, true);
  assert.equal(h.element("agent-progress").textContent, "");
  assert.equal(h.element("agent-form").attributes["aria-busy"], "false");
  assert.equal(h.element("agent-answer").textContent, "库存结果");

  h.submit();
  await flush();
  h.waiting.shift().reject(new Error("query failed"));
  await flush();
  assert.equal(h.element("agent-progress").hidden, true);
  assert.equal(h.element("agent-form").attributes["aria-busy"], "false");
});

test("an old session cannot clear the new session's pending indicator", async () => {
  const h = agentHarness();
  h.submit();
  await flush();
  const oldRequest = h.waiting.shift();
  h.context.clearSession("已退出");
  assert.equal(h.element("agent-progress").hidden, true);
  h.context.active = h.session("buyer-b");
  h.element("prompt").value = "查询库存";
  h.submit();
  await flush();
  const newRequest = h.waiting.shift();
  assert.equal(h.element("agent-progress").hidden, false);
  oldRequest.resolve({...answer, answer: "旧会话"});
  await flush();
  assert.equal(h.element("agent-progress").hidden, false);
  assert.equal(h.element("agent-form").attributes["aria-busy"], "true");
  assert.notEqual(h.element("agent-answer").textContent, "旧会话");
  newRequest.resolve(answer);
  await flush();
  assert.equal(h.element("agent-progress").hidden, true);
  assert.equal(h.element("agent-answer").textContent, "库存结果");
});

test("switching during request preparation does not start an old session query", async () => {
  const h = agentHarness();
  let finishOldPreparation;
  h.context.pendingRequest = () => new Promise(resolve => { finishOldPreparation = resolve; });
  h.submit();
  assert.equal(h.element("agent-progress").hidden, false);
  h.context.clearSession("已退出");
  h.context.active = h.session("buyer-b");
  h.element("prompt").value = "查询库存";
  h.context.pendingRequest = async () => "new-request-id";
  h.submit();
  h.submit(); // A second submission while busy must not issue another query.
  await flush();
  assert.equal(h.waiting.length, 1);
  finishOldPreparation("old-request-id");
  await flush();
  assert.equal(h.waiting.length, 1);
  assert.equal(h.element("agent-progress").hidden, false);
  h.waiting.shift().resolve(answer);
  await flush();
  assert.equal(h.element("agent-progress").hidden, true);
  assert.equal(h.element("agent-answer").textContent, "库存结果");
});
