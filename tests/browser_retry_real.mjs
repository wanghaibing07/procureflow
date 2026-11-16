// Run with ego-browser nodejs -e "await import('file:///ABSOLUTE/PATH/procureflow/tests/browser_retry_real.mjs')"
// Uses a new temporary SQLite database and a real local ProcureFlow API, with a
// loopback proxy that drops the first successful manual and model-assisted
// draft responses. The model is a deterministic localhost fake.
const http = await import('node:http');
const fs = await import('node:fs/promises');
const os = await import('node:os');
const path = await import('node:path');
const url = await import('node:url');
const {spawn} = await import('node:child_process');
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
    ? {role: 'assistant', content: null, tool_calls: [{id: 'call-local-draft', type: 'function',
        function: {name: 'create_draft', arguments: JSON.stringify({skus: ['BRACKET']})}}]}
    : {role: 'assistant', content: 'The requested operation is complete.'};
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
  proxy = http.createServer(async (req, res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const data = Buffer.concat(chunks);
      const headers = {...req.headers};
      delete headers.host;
      delete headers['content-length'];
      const result = await fetch(`${backendOrigin}${req.url}`, {method: req.method, headers,
        ...(data.length ? {body: data} : {}), redirect: 'manual'});
      const bytes = Buffer.from(await result.arrayBuffer());
      if (req.method === 'POST' && req.url === '/api/drafts') {
        const body = JSON.parse(bytes.toString('utf8'));
        if (!result.ok) throw new Error(`Draft creation failed: ${result.status} ${JSON.stringify(body)}`);
        attempts.push({key: req.headers['idempotency-key'], draft: body.draft_id});
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
  const entry = (await listTaskSpaces()).find(s => s.name === 'ProcureFlow retry verification');
  const task = !entry ? await taskSpace('ProcureFlow retry verification')
    : entry.ownership === 'user' ? await claimTaskSpace(entry.id)
    : entry.ownership === 'agentDelegatedToUser' ? await takeOverTaskSpace(entry.id) : await taskSpace(entry.id);
  const page = (await task.pages())[0] ?? (await task.adopt((await task.tabs())[0].page));
  console.log({taskSpaceId: task.spaceId});
  const signIn = async () => {
    await page.fill('#token', buyer);
    await page.click('#signin button', {label: '登录测试采购员'});
    await page.waitForSelector('#recommendations input[value="BRACKET"]', {state: 'visible'});
  };
  await page.goto(origin);
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: '选择测试物料'});
  await page.click('#create-draft', {label: '首次创建草案'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  const persisted = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (attempts.length !== 1 || persisted.length !== 1 || persisted[0].draft_id !== attempts[0].draft)
    throw new Error('Backend did not persist exactly one draft before response loss');
  await page.reload();
  await signIn();
  await page.click('#recommendations input[value="BRACKET"]', {label: '重新选择测试物料'});
  await page.click('#create-draft', {label: '重试创建草案'});
  await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已就绪'));
  const final = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (attempts.length !== 2 || attempts[0].key !== attempts[1].key || attempts[0].draft !== attempts[1].draft ||
      final.length !== 1 || final[0].draft_id !== attempts[0].draft)
    throw new Error('Retry changed the key or produced a duplicate draft');

  await page.fill('#prompt', '为 BRACKET 创建采购草案');
  await page.click('#allow-draft', {label: '授权助手创建草案'});
  await page.click('#agent-form button[type="submit"]', {label: '首次运行测试助手'});
  await page.waitForFunction(() => document.querySelector('#notice').classList.contains('error'));
  const agentPersisted = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (agentAttempts.length !== 1 || !agentAttempts[0].draft || agentPersisted.length !== 2 ||
      !agentPersisted.some(d => d.draft_id === agentAttempts[0].draft) || modelCalls.length !== 2 ||
      !agentAttempts[0].trace.events.some(e => e.tool === 'create_draft' && e.outcome === 'ok'))
    throw new Error(`Agent did not commit one draft through the model before response loss: ${JSON.stringify({attempts: agentAttempts.length, drafts: agentPersisted.length, calls: modelCalls.length, trace: agentAttempts[0]?.trace})}`);
  console.log('PASS agent draft committed before response loss');
  await page.reload();
  await signIn();
  await page.fill('#prompt', '为 BRACKET 创建采购草案');
  await page.click('#allow-draft', {label: '再次授权助手建单'});
  await page.click('#agent-form button[type="submit"]', {label: '重试运行测试助手'});
  await page.waitForFunction(() => !document.querySelector('#agent-result').hidden);
  const agentFinal = await fetch(`${backendOrigin}/api/drafts`, {headers: {authorization: `Bearer ${buyer}`}}).then(r => r.json());
  if (agentAttempts.length !== 2 || agentAttempts[0].requestId !== agentAttempts[1].requestId ||
      agentAttempts[0].draft !== agentAttempts[1].draft || agentFinal.length !== 2 ||
      !agentFinal.some(d => d.draft_id === agentAttempts[0].draft) || modelCalls.length !== 2 ||
      agentAttempts[1].trace.model_calls !== 0)
    throw new Error('Agent retry changed request or draft, duplicated the draft, or called the model again');
  console.log({realApi: 'pass', sqlite: 'pass', manualResponseLoss: 'pass', agentResponseLoss: 'pass',
    originalAgentRequestAndDraft: true, agentRetryModelCalls: 0, totalDrafts: agentFinal.length});
} finally {
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
