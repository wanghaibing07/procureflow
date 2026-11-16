// Synthetic loopback server exercises browser state and retries. Run with ego-browser nodejs.
const http = await import("node:http");
const fs = await import("node:fs/promises");
const cryptoNode = await import("node:crypto");
const pageHtml = await fs.readFile(new URL("../procureflow/web/index.html", import.meta.url));
const attempts = {manual: [], agent: []};
const drafts = new Map();
const stock = {sku: "BRACKET", name: "Demo item", on_hand: 1, reserved: 0, inbound_by_due: 0,
  forecast_demand: 2, safety_stock: 0, shortage: 1};
const line = {sku: "BRACKET", quantity: 1, supplier_id: "S-FAST", unit_price: "1.00", due_date: "2026-01-08"};
const originalHash = "a".repeat(64);
const changedHash = "b".repeat(64);
let previewCalls = 0;
let staleOnce = false;
let consumeOnce = false;
const consumedPreviews = new Set();
const committedPreviews = new Map();
const cancelledPreviews = new Set();
let cancelFailsOnce = false;
let hideRecommendation = false;
let pauseCancel = false, releaseCancel, announceCancel;
let cancelStarted = null;
const server = http.createServer(async (req, res) => {
  const json = (data, status = 200) => { res.writeHead(status, {"content-type": "application/json"}); res.end(JSON.stringify(data)); };
  const url = new URL(req.url, "http://localhost");
  const actor = req.headers.authorization === "Bearer test-buyer-b" ? "buyer-b" : "buyer-a";
  if (url.pathname === "/") { res.writeHead(200, {"content-type": "text/html; charset=utf-8"}); res.end(pageHtml); return; }
  if (url.pathname === "/healthz") return json({ready: true, model_configured: true});
  if (url.pathname === "/api/me") return json({actor_id: actor, role: "buyer"});
  if (url.pathname === "/api/stock") return json([stock]);
  if (url.pathname === "/api/recommendations") return json([{stock, line: hideRecommendation ? null : line}]);
  if (url.pathname === "/api/drafts" && req.method === "GET") return json([...drafts.values()].filter((d) => d.buyer_id === actor));
  if (req.method === "POST" && ["/api/drafts/preview", "/api/drafts/preview/cancel", "/api/drafts", "/api/agent/run"].includes(url.pathname)) {
    let text = "";
    for await (const chunk of req) text += chunk;
    const body = JSON.parse(text);
    if (url.pathname === "/api/drafts/preview") {
      previewCalls += 1;
      return json({skus: body.skus, as_of: body.as_of, horizon_days: body.horizon_days,
        lines: [line], total: "1.00", preview_hash: previewCalls === 1 ? originalHash : previewCalls === 2 ? changedHash
          : cryptoNode.createHash('sha256').update(String(previewCalls)).digest('hex')});
    }
    if (url.pathname === "/api/drafts/preview/cancel") {
      if (pauseCancel) {
        pauseCancel = false;
        announceCancel();
        await new Promise(resolve => { releaseCancel = resolve; });
      }
      if (cancelFailsOnce) { cancelFailsOnce = false; return json({detail: 'temporary failure'}, 503); }
      if (committedPreviews.has(body.preview_hash)) return json({status: 'committed', draft: committedPreviews.get(body.preview_hash)});
      cancelledPreviews.add(body.preview_hash);
      return json({status: 'cancelled', draft: null});
    }
    const manual = url.pathname === "/api/drafts";
    const kind = manual ? "manual" : "agent";
    const id = manual ? req.headers["idempotency-key"] : body.request_id;
    attempts[kind].push({actor, id, body});
    if (manual && consumeOnce) {
      consumeOnce = false;
      consumedPreviews.add(body.preview_hash);
      const otherDraft = {draft_id: cryptoNode.randomUUID(), buyer_id: actor,
        status: "draft", total: "1.00", lines: [line], events: []};
      drafts.set("other-request", otherDraft);
      committedPreviews.set(body.preview_hash, otherDraft);
      return json({detail: "The purchase preview changed. Review and confirm it again."}, 409);
    }
    if (manual && (consumedPreviews.has(body.preview_hash) || cancelledPreviews.has(body.preview_hash)))
      return json({detail: "The purchase preview changed. Review and confirm it again."}, 409);
    if (manual && staleOnce) {
      staleOnce = false;
      return json({detail: "The purchase preview changed. Review and confirm it again."}, 409);
    }
    const key = JSON.stringify([kind, actor, id]);
    if (manual && !drafts.has(key)) drafts.set(key, {draft_id: cryptoNode.randomUUID(), buyer_id: actor,
      status: "draft", total: "1.00", lines: [line], events: []});
    if (attempts[kind].length === 1) {
      res.writeHead(200, {"content-type": "application/json", "content-length": "1000"});
      res.write('{"incomplete":');
      setTimeout(() => res.destroy(), 100);
      return;
    }
    if (manual) return json(drafts.get(key));
    const legacy = body.prompt === "Legacy service response";
    const result = {request_id: id, draft_id: null,
      answer: body.prompt === "Explain the rules" ? "模型声称：到货日期已经确定。" : "Test query completed",
      trace: {model: "test-model", model_calls: 1, elapsed_ms: 1, input_tokens: 1, output_tokens: 1, events: []}};
    if (body.prompt === "Successful query") result.trace.events = [{tool: "list_stock", outcome: "ok", elapsed_ms: 1}];
    if (body.prompt === "Rejected query") result.trace.events = [{tool: "list_stock", outcome: "rejected", elapsed_ms: 1}];
    if (body.prompt === "Missing trace") result.trace = null;
    if (!legacy) result.authoritative_rules = [
      {code: "required_date", explanation: "需求截止日不是确认到货日期。"},
      {code: "draft_workflow", explanation: "采购员先确认预览，之后才生成草案。 <b>文本</b>"},
    ];
    return json(result);
  }
  res.writeHead(404); res.end();
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
const entry = (await listTaskSpaces()).find(s => s.name === "采购确认重试验收");
const task = entry ? await taskSpace(entry.id) : await taskSpace("采购确认重试验收");
const page = task.page("p1");
console.log({taskSpaceId: task.spaceId});
try {
  const signIn = async () => {
    await page.fill("#token", "test-buyer-a");
    await page.click("#signin button", {label: "登录测试采购员"});
    await page.waitForFunction(() => document.querySelector('#recommendations').children.length > 0);
  };
  const fillPrompt = async value => {
    await page.waitForSelector('#prompt', {state: 'visible'});
    await page.focus('#prompt');
    await page.keyboard.press('ControlOrMeta+A');
    await page.keyboard.insertText(value);
  };
  const selectAndPreview = async () => {
    await page.click('#recommendations input[value="BRACKET"]', {label: "选择演示物料"});
    await page.click("#create-draft", {label: "查看采购预览"});
    await page.waitForSelector("#draft-preview", {state: "visible"});
  };
  const failed = async () => page.waitForFunction(() => document.querySelector("#notice").classList.contains("error"));
  await page.goto(origin);
  await signIn();
  await selectAndPreview();
  if (drafts.size || attempts.manual.length) throw new Error("Preview created a draft before confirmation");
  const displayed = await page.evaluate(() => document.querySelector("#draft-preview").innerText);
  if (!displayed.includes("BRACKET") || !displayed.includes("S-FAST") || !displayed.includes("1.00"))
    throw new Error("Preview omitted authoritative purchase details");
  await page.click("#confirm-draft", {label: "确认演示草案"});
  await failed();
  if (drafts.size !== 1) throw new Error("Response-loss fixture did not commit a draft");
  const firstStorage = await page.evaluate(async () => {
    const opened = indexedDB.open("procureflow-pending-v1");
    const db = await new Promise(resolve => { opened.onsuccess = () => resolve(opened.result); });
    const entries = await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readonly"), store = tx.objectStore("state");
      const keys = store.getAllKeys(), values = store.getAll();
      tx.oncomplete = () => resolve(keys.result.map((key, i) => [key, values.result[i]]));
      tx.onerror = () => reject(tx.error);
    });
    const record = entries.find(([key, value]) => String(key).startsWith("pending:") && value?.ciphertext);
    if (!record) throw new Error("Pending confirmation was not saved");
    const exposed = JSON.stringify(record[1]).includes("a".repeat(64));
    // Recreate the immediately preceding release's record shape to test safe migration.
    await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readwrite");
      tx.objectStore("state").put({...record[1], preview_hash: "a".repeat(64)}, record[0]);
      tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
    });
    db.close(); return {exposed};
  });
  if (firstStorage.exposed) throw new Error("Raw preview credential was written to browser storage");
  hideRecommendation = true;
  await page.reload();
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  const migrated = await page.evaluate(async () => {
    const opened = indexedDB.open("procureflow-pending-v1");
    const db = await new Promise(resolve => { opened.onsuccess = () => resolve(opened.result); });
    const values = await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readonly"), request = tx.objectStore("state").getAll();
      tx.oncomplete = () => resolve(request.result); tx.onerror = () => reject(tx.error);
    });
    db.close(); return !JSON.stringify(values).includes("a".repeat(64));
  });
  if (!migrated) throw new Error("Existing plaintext preview credential was not migrated");
  if (await page.evaluate(() => !!document.querySelector('#recommendations input[value="BRACKET"]')))
    throw new Error('Test fixture did not remove the selectable recommendation');
  await page.click('#pending-recovery button', {label: '查看待恢复请求'});
  const recovery = await page.evaluate(() => ({text: document.querySelector("#draft-preview").innerText,
    button: document.querySelector("#confirm-draft").innerText}));
  if (!recovery.text.includes("上次确认后结果未知") || !recovery.button.includes("找回") || previewCalls !== 1)
    throw new Error("Uncertain previous confirmation was not disclosed before a fresh preview");
  await page.click("#confirm-draft", {label: "找回原有草案"});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  if (attempts.manual.length !== 2 || attempts.manual[0].id !== attempts.manual[1].id ||
      attempts.manual[1].body.preview_hash !== originalHash || drafts.size !== 1)
    throw new Error("Retry changed the original confirmation or created a duplicate");
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden ||
      !document.querySelector('#recovered-lines').textContent.includes('BRACKET')))
    throw new Error('Replayed draft was not displayed with its pending record intact');
  await page.waitForFunction(() => !document.querySelector('#switch-user').disabled);
  await page.click('#switch-user', {label: '切换后核对草案'});
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.click('#pending-recovery button', {label: '再次查看待恢复'});
  await page.click('#confirm-draft', {label: '重试原有草案'});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  if (attempts.manual.length !== 3 || attempts.manual[2].id !== attempts.manual[0].id || drafts.size !== 1)
    throw new Error('Switching identities lost replay state or duplicated the draft');
  await page.evaluate(() => {
    const original = indexedDB.open.bind(indexedDB);
    window.__restoreReplayStorage = () => { indexedDB.open = original; };
    indexedDB.open = () => { throw new Error('Synthetic storage failure'); };
  });
  await page.click('#recovered-ack', {label: '模拟恢复存储失败'});
  await failed();
  if (await page.evaluate(() => document.querySelector('#recovered-result').hidden ||
      document.querySelector('#pending-recovery').hidden || document.querySelector('#switch-user').disabled))
    throw new Error('Replay acknowledgement failure lost the draft result or recovery record');
  await page.evaluate(() => window.__restoreReplayStorage());
  await page.click('#recovered-ack', {label: '确认已查看草案'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已确认恢复的草案'));
  hideRecommendation = false;
  await page.click('#reload-stock', {label: '刷新演示库存'});
  await page.waitForSelector('#recommendations input[value="BRACKET"]', {state: 'visible'});

  await fillPrompt("Find the demo stock");
  await page.click('#agent-form button[type="submit"]', {label: "首次查询助手"});
  await failed();
  await page.reload();
  await signIn();
  await fillPrompt("Find the demo stock");
  await page.click('#agent-form button[type="submit"]', {label: "重试助手查询"});
  await page.waitForFunction(() => !document.querySelector("#agent-result").hidden);
  if (attempts.agent.length !== 2 || attempts.agent[0].id !== attempts.agent[1].id ||
      attempts.agent.some(a => a.body.allow_draft !== false) || drafts.size !== 1)
    throw new Error("Agent retry or read-only boundary failed");
  const replayRules = await page.evaluate(() => ({visible: !document.querySelector('#agent-result').hidden,
    heading: document.querySelector('#system-rules-title').textContent,
    rules: document.querySelector('#system-rules-list').textContent,
    fallback: !document.querySelector('#system-rules-unavailable').hidden}));
  if (!replayRules.visible || replayRules.heading !== '系统规则' || replayRules.fallback ||
      !replayRules.rules.includes('需求截止日不是确认到货日期'))
    throw new Error('Server rules were not displayed for a replayed request');

  await fillPrompt('Explain the rules');
  await page.click('#agent-form button[type="submit"]', {label: '查询系统规则'});
  await page.waitForFunction(() => document.querySelector('#agent-answer').textContent.includes('模型声称'));
  const normalRules = await page.evaluate(() => ({answer: document.querySelector('#agent-answer').textContent,
    rules: document.querySelector('#system-rules-list').textContent,
    labels: document.querySelector('#agent-result').textContent,
    markup: document.querySelector('#system-rules-list b') !== null,
    fallback: !document.querySelector('#system-rules-unavailable').hidden}));
  if (!normalRules.answer.includes('到货日期已经确定') || !normalRules.rules.includes('不是确认到货日期') ||
      !normalRules.labels.includes('模型回答可能有误') || normalRules.markup || normalRules.fallback)
    throw new Error('Model answer and authoritative rules were not safely distinguished');

  for (const [prompt, expected] of [
    ['Rejected query', '没有成功查询业务数据'],
    ['Successful query', '不能证明回答中每个结论都有依据'],
    ['Missing trace', '无法确认本次查询依据'],
  ]) {
    await page.waitForFunction(() => !document.querySelector('#agent-form button[type="submit"]').disabled);
    await fillPrompt(prompt);
    await page.click('#agent-form button[type="submit"]', {label: '检查查询依据提示'});
    await page.waitForFunction(value => !document.querySelector('#agent-result').hidden &&
      document.querySelector('#agent-evidence').textContent.includes(value), expected);
  }

  await page.waitForFunction(() => !document.querySelector('#agent-form button[type="submit"]').disabled);
  await fillPrompt('Legacy service response');
  await page.click('#agent-form button[type="submit"]', {label: '测试旧版响应'});
  await page.waitForFunction(() => !document.querySelector('#agent-result').hidden &&
    !document.querySelector('#system-rules-unavailable').hidden);
  if (await page.evaluate(() => document.querySelector('#system-rules-list').childElementCount !== 0))
    throw new Error('Missing server rules reused stale rules from a previous response');

  // A legacy opaque pending ID cannot safely be attributed to a specific old
  // purchase; surface it and require the user to inspect drafts first.
  await page.evaluate(async () => {
    const opened = indexedDB.open("procureflow-pending-v1");
    const db = await new Promise(resolve => { opened.onsuccess = () => resolve(opened.result); });
    await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readwrite");
      tx.objectStore("state").put(crypto.randomUUID(), "pending:" + "f".repeat(64));
      tx.oncomplete = resolve; tx.onerror = () => reject(tx.error);
    });
    db.close();
  });
  await page.reload();
  await signIn();
  await page.waitForSelector('#legacy-recovery', {state: 'visible'});
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择演示物料'});
  if (!(await page.evaluate(() => document.querySelector('#create-draft').disabled)))
    throw new Error('Legacy pending request was silently ignored');
  await page.click('#legacy-ack', {label: '核对旧版草案'});
  await page.click('#create-draft', {label: '查看采购预览'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  const staleStart = attempts.manual.length;
  staleOnce = true;
  await page.click("#confirm-draft", {label: "确认过期预览"});
  await failed();
  const conflict = await page.evaluate(() => ({text: document.querySelector('#notice').textContent,
    shown: !document.querySelector('#resolve-conflict').hidden,
    disabled: document.querySelector('#resolve-pending').disabled,
    retained: !document.querySelector('#pending-recovery').hidden}));
  if (drafts.size !== 1 || !conflict.shown || !conflict.disabled || !conflict.retained ||
      !conflict.text.includes('可能已存在') || conflict.text.includes('未生成草案'))
    throw new Error("The uncertain conflict was misreported or its recovery record lost");
  await page.click('#resolve-ack', {label: '确认已核对草案'});
  cancelFailsOnce = true;
  await page.click('#resolve-pending', {label: '解除本地恢复记录'});
  await failed();
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden))
    throw new Error('A failed server cancellation erased the pending confirmation');
  await page.click('#resolve-pending', {label: '重试服务端核实'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('服务端已取消'));
  await page.click("#create-draft", {label: "重新获取预览"});
  await page.waitForSelector("#draft-preview", {state: "visible"});
  await page.click("#confirm-draft", {label: "确认新预览"});
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("已就绪"));
  if (drafts.size !== 2 || attempts.manual[staleStart].id === attempts.manual[staleStart + 1].id ||
      attempts.manual[staleStart].body.preview_hash === attempts.manual[staleStart + 1].body.preview_hash)
    throw new Error("Stale confirmation was not renewed");

  await selectAndPreview();
  consumeOnce = true;
  await page.click('#confirm-draft', {label: '模拟并发消费预览'});
  await failed();
  const consumed = await page.evaluate(() => ({text: document.querySelector('#notice').textContent,
    retained: !document.querySelector('#pending-recovery').hidden,
    hasDraft: document.querySelector('#drafts').innerText.includes('采购单')}));
  if (drafts.size !== 3 || !consumed.retained || !consumed.hasDraft || !consumed.text.includes('可能已存在'))
    throw new Error('Consumed preview conflict was falsely reported as an absence of drafts');
  await page.reload();
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.click('#pending-recovery button', {label: '查看待恢复请求'});
  await page.click('#confirm-draft', {label: '再次查找原有草案'});
  await failed();
  if (drafts.size !== 3 || !(await page.evaluate(() => !document.querySelector('#pending-recovery').hidden)))
    throw new Error('Consumed-preview recovery did not preserve the original request after reload');
  await page.click('#resolve-ack', {label: '确认已核对草案'});
  pauseCancel = true;
  cancelStarted = new Promise(resolve => { announceCancel = resolve; });
  await page.click('#resolve-pending', {label: '核实已有草案'});
  await Promise.race([cancelStarted, new Promise((_, reject) => setTimeout(() => reject(new Error('Cancellation request did not start')), 10000))]);
  const networkLock = await page.evaluate(() => {
    const disabled = document.querySelector('#switch-user').disabled;
    document.querySelector('#switch-user').click();
    return {disabled, stillSignedIn: document.querySelector('#signin').hidden,
      pending: !document.querySelector('#pending-recovery').hidden};
  });
  if (!networkLock.disabled || !networkLock.stillSignedIn || !networkLock.pending)
    throw new Error('Identity could switch while server resolution was in flight');
  releaseCancel();
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden))
    throw new Error('The pending record was erased before the committed result was acknowledged');
  // After the result is visible, switching identities must not discard it.
  await page.click('#switch-user', {label: '切换采购员身份'});
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.click('#pending-recovery button', {label: '查看待恢复请求'});
  await page.click('#resolve-ack', {label: '确认已核对草案'});
  await page.click('#resolve-pending', {label: '再次核实已有草案'});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  const recovered = await page.evaluate(() => ({summary: document.querySelector('#recovered-summary').textContent,
    details: document.querySelector('#recovered-lines').textContent}));
  if (!recovered.summary.includes('1.00') || !recovered.details.includes('BRACKET') || !recovered.details.includes('S-FAST'))
    throw new Error('The committed authoritative draft details were not visible');
  await page.evaluate(() => {
    const original = indexedDB.open.bind(indexedDB);
    window.__restorePendingStorage = () => { indexedDB.open = original; };
    indexedDB.open = () => { throw new Error('Synthetic storage failure'); };
  });
  await page.click('#recovered-ack', {label: '模拟恢复存储失败'});
  await failed();
  if (await page.evaluate(() => document.querySelector('#recovered-result').hidden ||
      document.querySelector('#pending-recovery').hidden || document.querySelector('#switch-user').disabled))
    throw new Error('Storage failure lost the visible result or left identity locked');
  await page.evaluate(() => window.__restorePendingStorage());
  // Simulate slow local key/IndexedDB preparation after the user acknowledges.
  await page.evaluate(() => {
    const original = crypto.subtle.generateKey.bind(crypto.subtle);
    window.__restoreGenerateKey = () => { crypto.subtle.generateKey = original; };
    let hold = true;
    crypto.subtle.generateKey = async (...args) => {
      if (hold) { hold = false; await new Promise(resolve => { window.__releaseSlowStorage = resolve; }); }
      return original(...args);
    };
  });
  await page.click('#recovered-ack', {label: '确认已查看草案'});
  await page.waitForFunction(() => typeof window.__releaseSlowStorage === 'function');
  const storageLock = await page.evaluate(() => {
    const disabled = document.querySelector('#switch-user').disabled;
    document.querySelector('#switch-user').click();
    return {disabled, stillSignedIn: document.querySelector('#signin').hidden,
      pending: !document.querySelector('#pending-recovery').hidden};
  });
  if (!storageLock.disabled || !storageLock.stillSignedIn || !storageLock.pending)
    throw new Error('Identity could switch while acknowledgement persistence was in flight');
  await page.evaluate(() => { window.__releaseSlowStorage(); window.__restoreGenerateKey(); });
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已确认恢复的草案'));
  await page.click('#legacy-ack', {label: '核对旧版草案'});

  await selectAndPreview();
  await page.evaluate(() => pendingRequest("confirmation", "buyer-a",
    {skus: ["BRACKET"], as_of: "2026-01-01", horizon_days: 7}, undefined, "c".repeat(64)));
  const beforeRace = attempts.manual.length;
  await page.click("#confirm-draft", {label: "核对并发确认"});
  await page.waitForFunction(() => document.querySelector("#preview-help").textContent.includes("上次确认后结果未知"));
  if (attempts.manual.length !== beforeRace)
    throw new Error("A different pending confirmation was sent without an explicit recovery decision");

  const isolation = await page.evaluate(async () => {
    const body = {skus: ["BRACKET"], as_of: "2026-01-01", horizon_days: 7};
    const hash = "c".repeat(64);
    const [a, concurrent, b, other] = await Promise.all([
      pendingRequest("confirmation", "test-actor-a", body, undefined, hash),
      pendingRequest("confirmation", "test-actor-a", body, undefined, hash),
      pendingRequest("confirmation", "test-actor-b", body, undefined, hash),
      pendingRequest("confirmation", "test-actor-a", {...body, horizon_days: 8}, undefined, hash),
    ]);
    await pendingRequest("confirmation", "test-actor-a", body, a.id);
    const next = await pendingRequest("confirmation", "test-actor-a", body, undefined, hash);
    return {sameConcurrent: a.id === concurrent.id, distinctActors: a.id !== b.id,
      distinctBodies: a.id !== other.id, renewed: next.id !== a.id,
      distinguishesExisting: a.already_present !== concurrent.already_present && !next.already_present};
  });
  if (Object.values(isolation).some(value => !value)) throw new Error("Pending request isolation failed");
  const storage = await page.evaluate(async () => {
    const opened = indexedDB.open("procureflow-pending-v1");
    const db = await new Promise((resolve, reject) => { opened.onsuccess = () => resolve(opened.result); opened.onerror = () => reject(opened.error); });
    const result = await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readonly"), store = tx.objectStore("state");
      const keys = store.getAllKeys(), values = store.getAll();
      tx.oncomplete = () => resolve({keys: keys.result, values: values.result.map(value => typeof value === "string" ? value
        : value?.id ? value : {type: value.type, extractable: value.extractable})});
      tx.onerror = () => reject(tx.error);
    });
    db.close(); return result;
  });
  if (!storage.keys.every(key => ["hmac-key", "aes-key"].includes(key) || /^pending:[0-9a-f]{64}$/.test(key)) ||
      !storage.values.every(value => typeof value === "string" ? /^[0-9a-f-]{36}$/.test(value)
        : value.id ? /^[0-9a-f-]{36}$/.test(value.id)
          && /^[0-9a-f]{24}$/.test(value.iv) && /^(?:[0-9a-f]{2})+$/.test(value.ciphertext)
          && Object.keys(value).sort().join(",") === "ciphertext,id,iv"
          : value.type === "secret" && value.extractable === false))
    throw new Error("Unexpected data in pending request store");
  await page.cdp("Emulation.setDeviceMetricsOverride", {width: 390, height: 844, deviceScaleFactor: 1, mobile: true});
  const mobile = await page.evaluate(() => ({viewport: innerWidth, width: document.documentElement.scrollWidth,
    preview: !document.querySelector("#draft-preview").hidden,
    recoveryLabel: document.querySelector("#confirm-draft").textContent}));
  if (mobile.width > mobile.viewport || !mobile.preview || !mobile.recoveryLabel.includes("找回"))
    throw new Error("Recovery preview is not usable at mobile width");
  const agentRequestsBeforeStorageFailure = attempts.agent.length;
  await page.evaluate(() => { indexedDB.open = () => { throw new Error("Storage disabled"); }; });
  await fillPrompt("A different test request");
  await page.click('#agent-form button[type="submit"]', {label: "检查存储不可用"});
  await page.waitForFunction(() => document.querySelector("#notice").textContent.includes("无法安全保存"));
  if (attempts.agent.length !== agentRequestsBeforeStorageFailure)
    throw new Error("Request was sent without persistent retry protection");
  console.log({previewBeforeWrite: "pass", responseLossRecovery: "pass", stalePreview: "pass",
    consumedConflictRecovery: "pass", sessionSwitchRace: "pass", migratedEncryptedPreview: "pass", agentReadOnlyRetry: "pass", authoritativeRules: "pass",
    isolation, storage: "opaque and fail-closed", mobile: "390px", syntheticDrafts: drafts.size});
} finally {
  if (releaseCancel) releaseCancel();
  server.closeAllConnections();
  await new Promise(resolve => server.close(resolve));
}
