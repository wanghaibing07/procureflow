// Run with ego-browser nodejs -e "await import('file:///ABSOLUTE/PATH/procureflow/tests/browser_retry_real.mjs')"
// Uses a new temporary SQLite database and a real local ProcureFlow API, with a
// loopback proxy that drops the first successful confirmed draft and
// read-only Agent responses. The model is a deterministic localhost fake.
const http = await import('node:http');
const fs = await import('node:fs/promises');
const os = await import('node:os');
const path = await import('node:path');
const url = await import('node:url');
const {spawn} = await import('node:child_process');
const {randomUUID} = await import('node:crypto');
const root = path.resolve(path.dirname(url.fileURLToPath(import.meta.url)), '..');
const python = path.join(root, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
await fs.access(python);
const tempRoot = await fs.realpath(os.tmpdir());
const temp = await fs.mkdtemp(path.join(tempRoot, 'procureflow-real-retry-'));
const buyer = 'synthetic-buyer-token-123456';
const approver = 'synthetic-approver-token-123456';
const erp = 'synthetic-erp-token-123456';
const modelToken = 'synthetic-model-token-123456';
const modelCalls = [];
const model = http.createServer(async (req, res) => {
  if (req.method !== 'POST' || req.url !== '/v1/chat/completions' ||
      req.headers.authorization !== `Bearer ${modelToken}`) {
    res.writeHead(404); res.end(); return;
  }
  const chunks = [];
  for await (const chunk of req) chunks.push(chunk);
  const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
  modelCalls.push(payload);
  const first = modelCalls.length === 1;
  const message = first
    ? {role: 'assistant', content: null, tool_calls: [{id: 'call-local-stock', type: 'function',
        function: {name: 'list_stock', arguments: '{}'}}]}
    : {role: 'assistant', content: 'The stock query is complete.'};
  const body = JSON.stringify({choices: [{message, finish_reason: first ? 'tool_calls' : 'stop'}],
    usage: {prompt_tokens: 1, completion_tokens: 1}});
  res.writeHead(200, {'content-type': 'application/json'}); res.end(body);
});
await new Promise(resolve => model.listen(0, '127.0.0.1', resolve));
const probe = http.createServer();
await new Promise(resolve => probe.listen(0, '127.0.0.1', resolve));
const backendPort = probe.address().port;
await new Promise(resolve => probe.close(resolve));
const backendOrigin = `http://127.0.0.1:${backendPort}`;
const dbFile = path.join(temp, 'retry.sqlite3').replaceAll('\\', '/');
const env = {...process.env, PROCUREFLOW_DATABASE_URL: `sqlite:///${dbFile}`,
  PROCUREFLOW_BUYER_TOKEN: buyer, PROCUREFLOW_APPROVER_TOKEN: approver, PROCUREFLOW_ERP_TOKEN: erp,
  PROCUREFLOW_MODEL_URL: `http://127.0.0.1:${model.address().port}/v1`, PROCUREFLOW_MODEL_API_KEY: modelToken,
  PROCUREFLOW_MODEL_NAME: 'synthetic-local-model', PROCUREFLOW_LOAD_DEMO: '1'};
if (process.platform === 'win32' && !env.SystemRoot) env.SystemRoot = 'C:\\Windows';
const app = spawn(python, ['-m', 'uvicorn', 'procureflow.api:app', '--host', '127.0.0.1', '--port', String(backendPort)],
  {cwd: root, env, stdio: ['ignore', 'ignore', 'pipe']});
let stderr = '';
app.stderr.on('data', chunk => { stderr += chunk.toString(); });
let proxy;
let releaseDelayed = null;
try {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    try { ready = (await fetch(`${backendOrigin}/healthz`)).ok; } catch {}
    if (ready || app.exitCode !== null) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (!ready) throw new Error(`Local API failed to start: ${stderr.slice(-1000)}`);
  const attempts = [];
  const agentAttempts = [];
  let lastPreview = null;
  let holdNextDraft = false, dropCancelResponse = false;
  let markDelayedStart, markDelayedResult;
  const delayedStart = new Promise(resolve => { markDelayedStart = resolve; });
  const delayedResult = new Promise(resolve => { markDelayedResult = resolve; });
  proxy = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const data = Buffer.concat(chunks);
      const headers = {...req.headers};
      delete headers.host;
      delete headers['content-length'];
      const delayed = req.method === 'POST' && req.url === '/api/drafts' && holdNextDraft;
      if (delayed) {
        holdNextDraft = false;
        markDelayedStart();
        await new Promise(resolve => { releaseDelayed = resolve; });
      }
      const result = await fetch(`${backendOrigin}${req.url}`, {method: req.method, headers,
        ...(data.length ? {body: data} : {}), redirect: 'manual'});
      const bytes = Buffer.from(await result.arrayBuffer());
      if (delayed) markDelayedResult(result.status);
      if (req.method === 'POST' && req.url === '/api/drafts/preview' && result.ok)
        lastPreview = JSON.parse(bytes.toString('utf8'));
      if (req.method === 'POST' && req.url === '/api/drafts') {
        const body = JSON.parse(bytes.toString('utf8'));
        attempts.push({key: req.headers['idempotency-key'], draft: body.draft_id, status: result.status});
        if (attempts.length === 1) {
          res.writeHead(200, {'content-type': 'application/json', 'content-length': String(bytes.length + 100)});
          res.write(bytes.subarray(0, 1));
          setTimeout(() => res.destroy(), 100);
          return;
        }
      }
      if (req.method === 'POST' && req.url === '/api/agent/run') {
        const requestBody = JSON.parse(data.toString('utf8'));
        const responseBody = JSON.parse(bytes.toString('utf8'));
        if (!result.ok) throw new Error(`Agent request failed: ${result.status} ${JSON.stringify(responseBody)}`);
        agentAttempts.push({requestId: requestBody.request_id, draft: responseBody.draft_id, trace: responseBody.trace});
        if (agentAttempts.length === 1) {
          res.writeHead(200, {'content-type': 'application/json', 'content-length': String(bytes.length + 100)});
          res.write(bytes.subarray(0, 1));
          setTimeout(() => res.destroy(), 100);
          return;
        }
      }
      if (req.method === 'POST' && req.url === '/api/drafts/preview/cancel' && dropCancelResponse && result.ok) {
        dropCancelResponse = false;
        res.writeHead(200, {'content-type': 'application/json', 'content-length': String(bytes.length + 100)});
        res.write(bytes.subarray(0, 1));
        setTimeout(() => res.destroy(), 100);
        return;
      }
      if (res.destroyed) return;
      const outputHeaders = {};
      result.headers.forEach((value, key) => { if (!['content-length', 'transfer-encoding', 'connection'].includes(key)) outputHeaders[key] = value; });
      res.writeHead(result.status, outputHeaders);
      res.end(bytes);
    } catch (error) {
      if (!res.headersSent) res.writeHead(500, {'content-type': 'text/plain'});
      res.end(String(error));
    }
  });
  await new Promise(resolve => proxy.listen(0, '127.0.0.1', resolve));
  const origin = `http://127.0.0.1:${proxy.address().port}`;
  const entry = (await listTaskSpaces()).find(s => s.name === '采购真实接口重试验收');
  const task = entry ? await taskSpace(entry.id) : await taskSpace('采购真实接口重试验收');
  const page = task.page('p1');
  console.log({taskSpaceId: task.spaceId});
  const signIn = async () => {
    await page.waitForSelector('#token', {state: 'visible'});
    console.log('sign-in form visible');
    await page.fill('#token', buyer);
    await page.click('#signin button', {label: '登录测试采购员'});
    await page.waitForSelector('#recommendations input[value="BRACKET"]', {state: 'visible'});
  };
  await page.goto(origin);
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择测试物料'});
  await page.click('#create-draft', {label: '查看采购预览'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  const beforeConfirm = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (beforeConfirm.length !== 0) throw new Error('A draft was created before confirmation');
  await page.click('#confirm-draft', {label: '首次确认草案'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  const persisted = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (attempts.length !== 1 || persisted.length !== 1 || persisted[0].draft_id !== attempts[0].draft)
    throw new Error('Backend did not persist exactly one draft before response loss');
  await page.reload();
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: '重新选择测试物料'});
  await page.click('#create-draft', {label: '查看待恢复请求'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  if (!(await page.evaluate(() => document.querySelector('#preview-help').textContent.includes('上次确认后结果未知'))))
    throw new Error('The uncertainty of the original request was not disclosed before retry');
  await page.click('#confirm-draft', {label: '恢复原有草案'});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden ||
      !document.querySelector('#recovered-lines').textContent.includes('BRACKET')))
    throw new Error('Replayed draft was not displayed with its pending record intact');
  await page.waitForFunction(() => !document.querySelector('#switch-user').disabled);
  await page.click('#switch-user', {label: '切换后核对草案'});
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.focus('#pending-recovery button');
  await page.keyboard.press('Enter');
  await page.click('#confirm-draft', {label: '再次重试原草案'});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  await page.evaluate(() => {
    const original = indexedDB.open.bind(indexedDB);
    window.__restoreReplayStorage = () => { indexedDB.open = original; };
    indexedDB.open = () => { throw new Error('Synthetic storage failure'); };
  });
  await page.click('#recovered-ack', {label: '模拟恢复存储失败'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  if (await page.evaluate(() => document.querySelector('#recovered-result').hidden ||
      document.querySelector('#pending-recovery').hidden || document.querySelector('#switch-user').disabled))
    throw new Error('Replay acknowledgement storage failure lost its result or recovery record');
  await page.evaluate(() => window.__restoreReplayStorage());
  await page.click('#recovered-ack', {label: '确认已查看草案'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已确认恢复的草案'));
  const final = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (attempts.length !== 3 || attempts.some(item => item.key !== attempts[0].key || item.draft !== attempts[0].draft) ||
      final.length !== 1 || final[0].draft_id !== attempts[0].draft)
    throw new Error('Retry changed the key or produced a duplicate draft');

  // A second request consumes the preview before the browser confirmation.
  // The same 409 also covers expired/changed previews; the UI must not claim
  // that no draft exists or erase the pending recovery record.
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择并发测试物料'});
  await page.click('#create-draft', {label: '查看并发测试预览'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  if (!lastPreview?.preview_hash) throw new Error('Preview was not returned by the real API');
  const competing = await fetch(`${backendOrigin}/api/drafts`, {method: 'POST', headers: {
    authorization: `Bearer ${buyer}`, 'content-type': 'application/json', 'idempotency-key': randomUUID()},
    body: JSON.stringify({skus: ['BRACKET'], as_of: '2026-01-01', horizon_days: 7, preview_hash: lastPreview.preview_hash})});
  if (!competing.ok) throw new Error('Competing confirmation did not create a draft');
  await page.click('#confirm-draft', {label: '确认已消费预览'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  const status = await page.evaluate(() => ({text: document.querySelector('#notice').textContent,
    pending: !document.querySelector('#pending-recovery').hidden, resolve: !document.querySelector('#resolve-conflict').hidden}));
  if (!status.text.includes('可能已存在') || status.text.includes('未生成草案') || !status.pending || !status.resolve)
    throw new Error('Consumed-preview conflict was misreported or lost its recovery record');
  await page.reload();
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  console.log('pending recovery visible after conflict');
  await page.focus('#pending-recovery button');
  await page.keyboard.press('Enter');
  console.log('opened pending recovery');
  await page.click('#confirm-draft', {label: '重试原确认请求'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  if (attempts.at(-1)?.status !== 409 || !(await page.evaluate(() => !document.querySelector('#pending-recovery').hidden)))
    throw new Error('Consumed-preview conflict did not remain recoverable after reload');
  await page.click('#resolve-ack', {label: '确认已核对草案'});
  console.log('acknowledged conflict');
  await page.click('#resolve-pending', {label: '解除本地恢复记录'});
  await page.waitForSelector('#recovered-result', {state: 'visible'});
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden))
    throw new Error('Committed result lost its pending record before buyer acknowledgement');
  await page.click('#recovered-ack', {label: '确认已查看草案'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已确认恢复的草案'));

  // Hold a previously confirmed request before it reaches the API, lose the
  // cancellation response, then retry cancellation across a browser reload.
  // After a fresh confirmation succeeds, release the old request: it must 409.
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择延迟测试物料'});
  await page.click('#create-draft', {label: '查看延迟测试预览'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  holdNextDraft = true;
  await page.click('#confirm-draft', {label: '发送延迟确认请求'});
  await Promise.race([delayedStart, new Promise((_, reject) => setTimeout(() => reject(new Error('Delayed request did not reach proxy')), 10000))]);
  await page.reload();
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.focus('#pending-recovery button');
  await page.keyboard.press('Enter');
  await page.waitForSelector('#resolve-conflict', {state: 'visible'});
  await page.click('#resolve-ack', {label: '确认核对延迟请求'});
  dropCancelResponse = true;
  await page.click('#resolve-pending', {label: '核实并取消旧请求'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  if (await page.evaluate(() => document.querySelector('#pending-recovery').hidden))
    throw new Error('Lost cancellation response erased the pending request');
  await page.reload();
  await signIn();
  await page.waitForSelector('#pending-recovery button', {state: 'visible'});
  await page.focus('#pending-recovery button');
  await page.keyboard.press('Enter');
  await page.click('#resolve-ack', {label: '再次核对旧请求'});
  await page.click('#resolve-pending', {label: '重试服务端取消'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('服务端已取消'));
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择新的采购物料'});
  await page.click('#create-draft', {label: '查看新的采购预览'});
  await page.waitForSelector('#draft-preview', {state: 'visible'});
  await page.click('#confirm-draft', {label: '确认新的采购草案'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已就绪'));
  releaseDelayed();
  releaseDelayed = null;
  const lateStatus = await Promise.race([delayedResult, new Promise((_, reject) => setTimeout(() => reject(new Error('Delayed request did not finish')), 10000))]);
  const afterLate = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (lateStatus !== 409 || afterLate.length !== 3)
    throw new Error('A delayed old confirmation committed after server cancellation and a fresh purchase');

  await page.waitForSelector('#prompt', {state: 'visible'});
  await page.focus('#prompt');
  await page.keyboard.insertText('查询 BRACKET 的库存');
  await page.click('#agent-form button[type="submit"]', {label: '首次运行测试助手'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  const agentPersisted = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (agentAttempts.length !== 1 || agentAttempts[0].draft || agentPersisted.length !== 3 || modelCalls.length !== 2 ||
      !agentAttempts[0].trace.events.some(e => e.tool === 'list_stock' && e.outcome === 'ok') ||
      modelCalls.some(call => call.tools?.some(tool => tool.function?.name === 'create_draft')))
    throw new Error('Agent query exposed a write tool or altered the draft list');
  console.log('PASS agent read-only query before response loss');
  await page.reload();
  console.log('reloaded after agent response loss');
  await signIn();
  await page.waitForSelector('#prompt', {state: 'visible'});
  console.log('agent input visible after reload');
  await page.focus('#prompt');
  await page.keyboard.insertText('查询 BRACKET 的库存');
  await page.click('#agent-form button[type="submit"]', {label: '重试运行测试助手'});
  await page.waitForFunction(() => !document.querySelector('#agent-result').hidden);
  const rules = await page.evaluate(() => ({text: document.querySelector('#system-rules-list').textContent,
    fallback: !document.querySelector('#system-rules-unavailable').hidden,
    warning: document.querySelector('#agent-result').textContent.includes('模型回答可能有误')}));
  if (rules.fallback || !rules.warning || !rules.text.includes('需求截止日不是') || !rules.text.includes('采购员先选择物料'))
    throw new Error('The real API replay did not display authoritative rules separately');
  const agentFinal = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (agentAttempts.length !== 2 || agentAttempts[0].requestId !== agentAttempts[1].requestId ||
      agentAttempts[1].draft || agentFinal.length !== 3 || modelCalls.length !== 2 ||
      agentAttempts[1].trace.model_calls !== 0)
    throw new Error('Agent retry changed request, created a draft, or called the model again');
  console.log({realApi: 'pass', sqlite: 'pass', manualResponseLoss: 'pass', agentResponseLoss: 'pass',
    consumedPreviewConflict: 'pass', cancelledDelayedRequest: 'pass', cancellationResponseLoss: 'pass',
    replayRules: 'pass', readOnlyAgent: true, agentRetryModelCalls: 0, totalDrafts: agentFinal.length});
} finally {
  if (releaseDelayed) releaseDelayed();
  if (proxy) { proxy.closeAllConnections(); await new Promise(resolve => proxy.close(resolve)); }
  if (app.exitCode === null) {
    app.kill();
    await new Promise(resolve => { app.once('exit', resolve); setTimeout(resolve, 3000); });
  }
  // Remove only the fresh test directory created directly inside the temp root.
  if (path.dirname(temp) !== tempRoot || !path.basename(temp).startsWith('procureflow-real-retry-'))
    throw new Error('Refusing to remove an unexpected temporary directory');
  model.closeAllConnections(); await new Promise(resolve => model.close(resolve));
  // On Windows the venv launcher can exit just before its Python child releases SQLite.
  await fs.rm(temp, {recursive: true, force: true, maxRetries: 20, retryDelay: 250});
}
