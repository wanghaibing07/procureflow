// Run in ego-browser after installing it: ego-browser nodejs -e "await import('file:///.../tests/browser_retry.mjs')"
// Serves only synthetic data on loopback and drops the first successful response for each path.
const http = await import("node:http");
const fs = await import("node:fs/promises");
const cryptoNode = await import("node:crypto");
const pageHtml = await fs.readFile(new URL("../procureflow/web/index.html", import.meta.url));
const attempts = {manual: [], agent: []};
const drafts = new Map();
const stock = {sku: "BRACKET", name: "Demo item", on_hand: 1, reserved: 0, inbound_by_due: 0,
  forecast_demand: 2, safety_stock: 0, shortage: 1};
const line = {sku: "BRACKET", quantity: 1, supplier_id: "S-FAST", unit_price: "1.00", due_date: "2026-01-08"};
const server = http.createServer(async (req, res) => {
  const json = (data) => { res.writeHead(200, {"content-type": "application/json"}); res.end(JSON.stringify(data)); };
  const url = new URL(req.url, "http://localhost");
  const actor = req.headers.authorization === "Bearer test-buyer-b" ? "buyer-b" : "buyer-a";
  if (url.pathname === "/") { res.writeHead(200, {"content-type": "text/html; charset=utf-8"}); res.end(pageHtml); return; }
  if (url.pathname === "/healthz") return json({ready: true, model_configured: true});
  if (url.pathname === "/api/me") return json({actor_id: actor, role: "buyer"});
  if (url.pathname === "/api/stock") return json([stock]);
  if (url.pathname === "/api/recommendations") return json([{stock, line}]);
  if (url.pathname === "/api/drafts" && req.method === "GET") return json([...drafts.values()].filter((draft) => draft.buyer_id === actor));
  if (req.method === "POST" && ["/api/drafts", "/api/agent/run"].includes(url.pathname)) {
    let text = "";
    for await (const chunk of req) text += chunk;
    const body = JSON.parse(text);
    const kind = url.pathname === "/api/drafts" ? "manual" : "agent";
    const id = kind === "manual" ? req.headers["idempotency-key"] : body.request_id;
    attempts[kind].push({actor, id});
    const key = JSON.stringify([kind, actor, id]);
    if (!drafts.has(key)) drafts.set(key, {draft_id: cryptoNode.randomUUID(), buyer_id: actor, status: "draft", total: "1.00", lines: [line], events: []});
    // The backend has committed; simulate the client losing the successful response.
    if (attempts[kind].length === 1) {
      res.writeHead(200, {"content-type": "application/json", "content-length": "1000"});
      res.write('{"incomplete":');
      setTimeout(() => res.destroy(), 100);
      return;
    }
    const draft = drafts.get(key);
    return json(kind === "manual" ? draft : {draft_id: draft.draft_id, answer: "Test draft created", trace: {
      model: "test-model", model_calls: 1, elapsed_ms: 1, input_tokens: 1, output_tokens: 1, events: []}});
  }
  res.writeHead(404); res.end();
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const entry = (await listTaskSpaces()).find((s) => s.name === "ProcureFlow retry verification");
const task = !entry ? await taskSpace("ProcureFlow retry verification")
  : entry.ownership === "user" ? await claimTaskSpace(entry.id)
  : entry.ownership === "agentDelegatedToUser" ? await takeOverTaskSpace(entry.id) : await taskSpace(entry.id);
const page = (await task.pages())[0] ?? (await task.adopt((await task.tabs())[0].page));
console.log({taskSpaceId: task.spaceId, origin});
try {
  const signIn = async () => {
    await page.fill("#token", "test-buyer-a");
    await page.click("#signin button", {label: "验证测试采购员"});
    await page.waitForSelector('#recommendations input[value="BRACKET"]', {state: "visible"});
  };
  const failed = async () => page.waitForFunction(() => document.querySelector("#notice").classList.contains("error"));
  await page.goto(origin);
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: "选择演示物料"});
  await page.click("#create-draft", {label: "首次生成草案"});
  await failed();
  await page.reload();
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: "重新选择物料"});
  await page.click("#create-draft", {label: "重试生成草案"});
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("已就绪"));
  if (attempts.manual.length !== 2 || attempts.manual[0].id !== attempts.manual[1].id || drafts.size !== 1)
    throw new Error("Manual retry created a duplicate or changed its key");
  await page.fill("#prompt", "Create the demo draft");
  await page.click("#allow-draft", {label: "授权测试草案"});
  await page.click('#agent-form button[type="submit"]', {label: "首次运行助手"});
  await failed();
  await page.reload();
  await signIn();
  await page.fill("#prompt", "Create the demo draft");
  await page.click("#allow-draft", {label: "再次授权草案"});
  await page.click('#agent-form button[type="submit"]', {label: "重试运行助手"});
  await page.waitForFunction(() => !document.querySelector("#agent-result").hidden);
  if (attempts.agent.length !== 2 || attempts.agent[0].id !== attempts.agent[1].id || drafts.size !== 2)
    throw new Error("Agent retry created a duplicate or changed its request ID");
  const isolation = await page.evaluate(async () => {
    const body = {skus: ["BRACKET"], as_of: "2026-01-01", horizon_days: 7};
    const [a, concurrent, b, other] = await Promise.all([
      pendingRequest("manual", "test-actor-a", body), pendingRequest("manual", "test-actor-a", body),
      pendingRequest("manual", "test-actor-b", body),
      pendingRequest("manual", "test-actor-a", {...body, horizon_days: 8}),
    ]);
    await pendingRequest("manual", "test-actor-a", body, a);
    const next = await pendingRequest("manual", "test-actor-a", body);
    return {sameConcurrent: a === concurrent, distinctActors: a !== b, distinctBodies: a !== other, renewed: next !== a};
  });
  if (Object.values(isolation).some((value) => !value)) throw new Error("Pending request isolation failed");
  const storage = await page.evaluate(async () => {
    const opened = indexedDB.open("procureflow-pending-v1");
    const db = await new Promise((resolve, reject) => { opened.onsuccess = () => resolve(opened.result); opened.onerror = () => reject(opened.error); });
    const result = await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readonly");
      const store = tx.objectStore("state");
      const keys = store.getAllKeys();
      const values = store.getAll();
      tx.oncomplete = () => resolve({keys: keys.result, values: values.result.map((value) =>
        typeof value === "string" ? value : {type: value.type, extractable: value.extractable})});
      tx.onerror = () => reject(tx.error);
    });
    db.close();
    return result;
  });
  if (!storage.keys.every((key) => key === "hmac-key" || /^pending:[0-9a-f]{64}$/.test(key))
      || !storage.values.every((value) => typeof value === "string" ? /^[0-9a-f-]{36}$/.test(value)
        : value.type === "secret" && value.extractable === false))
    throw new Error("Unexpected data in pending request store");
  await page.evaluate(() => { indexedDB.open = () => { throw new Error("Storage disabled"); }; });
  await page.fill("#prompt", "A different test request");
  await page.click('#agent-form button[type="submit"]', {label: "检查存储不可用"});
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("无法安全保存"));
  if (attempts.agent.length !== 2) throw new Error("Request was sent without persistent retry protection");
  console.log({manual: "pass", agent: "pass", isolation, storage: "opaque and fail-closed", syntheticDrafts: drafts.size});
} finally {
  await new Promise((resolve) => server.close(resolve));
}
