const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

// Run the functions shipped by the page with controllable request timing.
const html = fs.readFileSync(path.join(__dirname, "../procureflow/web/index.html"), "utf8");
const start = html.indexOf("    function quoteDetails(");
const end = html.indexOf("    async function loadStock(", start);
assert.ok(start >= 0 && end > start, "quote functions are present");
const flush = () => new Promise(resolve => setImmediate(resolve));
const quote = price => ({sku: "BRACKET", supplier_id: "S-FAST", supplier_name: "准达配件",
  unit_price: price, lead_days: 4, min_order: 10});

function harness() {
  const pending = [];
  const doc = {activeElement: null};
  function node(tag, text = "", cls = "") {
    return {tag, textContent: text, cls, children: [], listeners: {}, attributes: {}, isConnected: true,
      append(...children) { this.children.push(...children); },
      replaceChildren(...children) { this.children = children; },
      setAttribute(name, value) { this.attributes[name] = value; },
      addEventListener(name, callback) { this.listeners[name] = callback; },
      focus() { doc.activeElement = this; }};
  }
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, node("div"));
    return nodes.get(id);
  };
  const buyer = {identity: {role: "buyer"}};
  const ctx = vm.createContext({node, element, active: buyer, selectedSkus: () => [], controls() {}, responses: {},
    request: (url, session) => new Promise((resolve, reject) => pending.push({url, session, resolve, reject})),
    amount: value => "¥" + value, clearSession: () => { ctx.active = null; }});
  vm.runInContext(html.slice(start, end), ctx);
  return {ctx, pending, doc, element, buyer};
}

function open(details) { details.open = true; details.listeners.toggle(); }
function close(details) { details.open = false; details.listeners.toggle(); }

test("all stock items, including those without a shortage, can show an empty quote result", async () => {
  const h = harness();
  h.ctx.renderStock([{sku: "LABEL", name: "包装标签", on_hand: 120, reserved: 20,
    inbound_by_due: 0, forecast_demand: 28, safety_stock: 20, shortage: 0}], []);
  const details = h.element("quote-items").children[0];
  assert.match(details.children[0].textContent, /LABEL/);
  open(details);
  const call = h.pending.shift();
  assert.equal(call.url, "/api/quotes/LABEL");
  call.resolve([]); await flush();
  assert.match(details.children[1].textContent, /没有报价/);
  assert.equal(details.children[3].textContent, "刷新报价");
  assert.equal(details.children[3].hidden, false);
});

test("failed requests can be retried without losing the focused button", async () => {
  const h = harness();
  const details = h.ctx.quoteDetails("BRACKET", "安装支架", h.buyer);
  open(details);
  h.pending.shift().reject(new Error("offline")); await flush();
  const button = details.children[3];
  assert.match(details.children[1].textContent, /加载失败/);
  assert.equal(button.textContent, "重新加载报价");
  button.focus(); button.listeners.click();
  assert.equal(details.children[3], button);
  assert.equal(h.doc.activeElement, button);
  assert.equal(button.disabled, undefined);
  assert.equal(button.attributes["aria-disabled"], "true");
  button.listeners.click();
  assert.equal(h.pending.length, 1, "duplicate clicks do not start another request");
  h.pending.shift().resolve([quote("11.50")]); await flush();
  assert.equal(details.children[3], button);
  assert.equal(h.doc.activeElement, button);
  assert.equal(button.textContent, "刷新报价");
  assert.equal(button.attributes["aria-disabled"], "false");
  const table = details.children[2].children[0].children[0];
  assert.equal(table.children[2].children[0].children[1].textContent, "¥11.50");
});

test("reopening and refreshing fetch current values", async () => {
  const h = harness();
  const details = h.ctx.quoteDetails("BRACKET", "安装支架", h.buyer);
  open(details); h.pending.shift().resolve([quote("11.50")]); await flush();
  close(details); open(details);
  assert.equal(h.pending.length, 1);
  h.pending.shift().resolve([quote("12.00")]); await flush();
  assert.equal(details.children[2].children[0].children[0].children[2].children[0].children[1].textContent, "¥12.00");
  details.children[3].listeners.click();
  assert.equal(h.pending.length, 1);
  h.pending.shift().resolve([]); await flush();
  assert.match(details.children[1].textContent, /没有报价/);
});

test("late responses cannot update a replaced list or clear the new identity", async () => {
  const h = harness();
  const old = h.ctx.quoteDetails("BRACKET", "安装支架", h.buyer);
  open(old); const oldRequest = h.pending.shift();
  old.isConnected = false;
  const replacement = h.ctx.quoteDetails("BRACKET", "安装支架", h.buyer);
  open(replacement); h.pending.shift().resolve([quote("12.00")]); await flush();
  oldRequest.resolve([quote("11.50")]); await flush();
  assert.equal(old.children[2].children.length, 0);
  assert.equal(replacement.children[2].children[0].children[0].children[2].children[0].children[1].textContent, "¥12.00");
  replacement.children[3].listeners.click();
  const stale = h.pending.shift();
  const approver = {identity: {role: "approver"}};
  h.ctx.active = approver;
  stale.reject(Object.assign(new Error("unauthorized"), {status: 401})); await flush();
  assert.equal(h.ctx.active, approver);
});
