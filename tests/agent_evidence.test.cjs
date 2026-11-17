const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

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
