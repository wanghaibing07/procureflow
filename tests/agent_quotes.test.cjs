const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "../procureflow/web/index.html"), "utf8");
const begin = html.indexOf("    function renderQuoteComparisons(comparisons) {");
const end = html.indexOf("    function renderTrace(trace) {", begin);
assert.ok(begin >= 0 && end > begin);

function harness() {
  function node(tag, text = "", cls = "") {
    return {tag, textContent: text, cls, children: [], attributes: {},
      append(...items) { this.children.push(...items); },
      replaceChildren(...items) { this.children = items; },
      setAttribute(key, value) { this.attributes[key] = value; }};
  }
  const output = node("div");
  const context = vm.createContext({node, element: () => output, amount: value => "¥" + value});
  vm.runInContext(html.slice(begin, end), context);
  return {context, output};
}

const comparison = () => ({sku: "BRACKET", comparison_quantity: 30, as_of: "2026-01-01", horizon_days: 7,
  catalog_sku_found: true, truncated: false, items: [
    {sku: "BRACKET", supplier_id: "S-FAST", supplier_name: "准达配件", unit_price: "11.50", lead_days: 4,
      min_order: 10, within_planning_window: true,
      requested_quantity_comparison: {requested_quantity: 30, order_quantity: 30, extra_units: 0, total_amount: "345.00"},
      shortage_comparison: {shortage: 20, order_quantity: 20, extra_units: 0, total_amount: "230.00"}},
    {sku: "BRACKET", supplier_id: "S-VOLUME", supplier_name: "量惠工业", unit_price: "8.00", lead_days: 6,
      min_order: 40, within_planning_window: true,
      requested_quantity_comparison: {requested_quantity: 30, order_quantity: 40, extra_units: 10, total_amount: "320.00"},
      shortage_comparison: {shortage: 20, order_quantity: 40, extra_units: 20, total_amount: "320.00"}},
  ]});

function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function strings(root) { return descendants(root).map(n => n.textContent).join(" "); }

test("renders server amounts and quantities for requested quantity and stock shortage", () => {
  const {context, output} = harness();
  context.renderQuoteComparisons([comparison()]);
  assert.equal(output.children[0].textContent, "本次报价计算");
  assert.match(html, /<h4>助手说明<\/h4>\s*<p id="agent-answer"/);
  const text = strings(output);
  for (const value of ["BRACKET", "2026-01-01", "7 天", "查询比较数量 30 件", "¥345.00", "¥320.00", "¥230.00", "缺口数量"]) assert.ok(text.includes(value), value);
  const wrapper = descendants(output).find(n => n.attributes.role === "region");
  assert.equal(wrapper.tabIndex, 0);
  assert.match(wrapper.attributes["aria-label"], /BRACKET/);
  const table = descendants(wrapper).find(n => n.tag === "table");
  assert.match(table.cls, /comparison-table/);
  assert.match(html, /\.comparison-table\s*\{[^}]*width:\s*max-content[^}]*min-inline-size:\s*100%/);
  assert.match(html, /\.comparison-table th:first-child\s*\{[^}]*min-inline-size:\s*12rem/);
  assert.ok(descendants(table).some(n => n.tag === "thead"));
  assert.ok(descendants(table).some(n => n.tag === "tbody"));
  assert.ok(descendants(table).some(n => n.tag === "th" && n.scope === "col"));
  assert.ok(descendants(table).some(n => n.tag === "th" && n.scope === "row"));
});

test("shows separate query contexts and handles empty, absent and truncated data", () => {
  const {context, output} = harness();
  const present = {...comparison(), comparison_quantity: null, truncated: true,
    items: comparison().items.map(item => ({...item, requested_quantity_comparison: null}))};
  const empty = {sku: "LABEL", comparison_quantity: null, as_of: "2026-01-01", horizon_days: 7,
    catalog_sku_found: true, truncated: false, items: []};
  const absent = {...empty, sku: "MISSING", catalog_sku_found: false};
  context.renderQuoteComparisons([present, empty, absent]);
  assert.equal(output.children.length, 4);
  assert.match(strings(output.children[1]), /仅展示部分报价/);
  assert.doesNotMatch(strings(output.children[1]), /指定量订购数/);
  assert.match(strings(output.children[2]), /当前目录没有报价/);
  assert.match(strings(output.children[3]), /当前目录未找到此物料/);
  assert.doesNotMatch(strings(output.children[3]), /没有报价/);
});

test("renders supplier text as text and rejects mismatched or malformed results", () => {
  const {context, output} = harness();
  const value = comparison();
  value.items[0].supplier_name = "<img src=x onerror=alert(1)>";
  context.renderQuoteComparisons([value]);
  assert.ok(descendants(output).some(n => n.tag === "th" && n.textContent.includes("<img src=x onerror=alert(1)>")));
  assert.ok(!descendants(output).some(n => n.tag === "img"));
  value.items[0].requested_quantity_comparison.requested_quantity = 99;
  context.renderQuoteComparisons([value]);
  assert.match(strings(output), /结果格式不完整/);
  assert.ok(!descendants(output).some(n => n.tag === "table"));
  context.renderQuoteComparisons(null);
  assert.equal(output.children.length, 0);
});
