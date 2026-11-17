// Manual hosted experiment only: fresh synthetic Compose services and a real
// model configured by the runner. Buyer and approver use demonstration identities.
// The harness must separately verify model provenance, TLS, MySQL, ERP storage,
// and service restart/cleanup; this script only observes the browser and API.
import { chromium } from 'playwright';
import { constants } from 'node:fs';
import { copyFile, mkdir, realpath, stat, unlink, writeFile } from 'node:fs/promises';
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from 'node:path';

const { PF_BASE_URL, PF_BUYER_TOKEN, PF_APPROVER_TOKEN, PF_EVIDENCE_DIR, PF_PRIVATE_VIDEO_DIR } = process.env;
const evidence = {
  status: 'running', stage: 'initialization',
  environment: 'Chromium against an isolated synthetic demo',
  model_answer_semantics: 'not evaluated; requires independent review',
  checks: {},
};
let browser, context, page, video, videoDir;

// Recording is opt-in and may only use a sibling of the private evidence
// directory inside this run's private scratch directory. It must never be
// placed directly in the harness's upload directory.
async function privateVideoDirectory() {
  if (!PF_PRIVATE_VIDEO_DIR) return null;
  if (!process.env.RUNNER_TEMP || !isAbsolute(PF_PRIVATE_VIDEO_DIR) || !isAbsolute(PF_EVIDENCE_DIR))
    throw new Error('invalid_video_directory');
  const runnerTemp = await realpath(process.env.RUNNER_TEMP);
  const evidenceDir = await realpath(PF_EVIDENCE_DIR);
  const parent = dirname(evidenceDir);
  const relativeParent = relative(runnerTemp, parent);
  if (!/^pf-private\.[A-Za-z0-9]{8}$/.test(relativeParent) || basename(evidenceDir) !== 'browser-evidence' ||
      dirname(resolve(PF_PRIVATE_VIDEO_DIR)) !== parent || resolve(PF_PRIVATE_VIDEO_DIR) === evidenceDir)
    throw new Error('invalid_video_directory');
  await mkdir(PF_PRIVATE_VIDEO_DIR, { recursive: true, mode: 0o700 });
  const target = await realpath(PF_PRIVATE_VIDEO_DIR);
  if (dirname(target) !== parent || target === evidenceDir || target.startsWith(evidenceDir + sep) ||
      ((await stat(target)).mode & 0o077) !== 0)
    throw new Error('invalid_video_directory');
  return target;
}

// The fixed input opacity is verified before either token fill.
async function prepareVideoPage(page) {
  await page.addStyleTag({ content: `
    #token { opacity: 0 !important; transition: none !important; animation: none !important; }
  ` });
}
async function videoMoment(page, selector, pauseMs = 1500) {
  if (!videoDir) return;
  if (selector) await page.locator(selector).scrollIntoViewIfNeeded();
  await page.waitForTimeout(pauseMs);
}
async function checkVideoMask(page) {
  if (!videoDir) return;
  const masked = await page.locator('#token').evaluate(el => {
    const style = getComputedStyle(el);
    return style.opacity === '0' && el.getAttribute('type') === 'password';
  });
  if (!masked) throw new Error('video_mask_failed');
}

function requireCheck(value, name) {
  if (!value) throw new Error(name);
  evidence.checks[name] = true;
}
function safeAnswer(value) {
  let text = String(value ?? '');
  if (text.length > 100000) throw new Error('answer_exceeds_evidence_limit');
  for (const secret of [PF_BUYER_TOKEN, PF_APPROVER_TOKEN, process.env.PF_MODEL_TOKEN,
    process.env.PROCUREFLOW_MODEL_API_KEY].filter(Boolean)) text = text.replaceAll(secret, '[redacted]');
  return text.replace(/Bearer\s+\S+/gi, 'Bearer [redacted]')
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----/g, '[redacted]');
}
async function writeEvidence() {
  if (!PF_EVIDENCE_DIR) return;
  await mkdir(PF_EVIDENCE_DIR, { recursive: true, mode: 0o700 });
  await writeFile(join(PF_EVIDENCE_DIR, 'browser-acceptance.json'), JSON.stringify(evidence, null, 2) + '\n', { mode: 0o600 });
}

try {
  if (!PF_BASE_URL || !PF_BUYER_TOKEN || !PF_APPROVER_TOKEN || !PF_EVIDENCE_DIR) throw new Error('missing_configuration');
  const url = new URL(PF_BASE_URL);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port || url.username || url.password ||
      !['', '/'].includes(url.pathname) || url.search || url.hash) throw new Error('invalid_origin');
  const origin = url.origin;

  async function api(method, path, token, body, key) {
    const response = await fetch(origin + path, { method, redirect: 'error',
      headers: { authorization: `Bearer ${token}`, ...(body ? { 'content-type': 'application/json' } : {}),
        ...(key ? { 'idempotency-key': key } : {}) }, ...(body ? { body: JSON.stringify(body) } : {}) });
    return { status: response.status, data: await response.json() };
  }
  async function signIn(page, token, label) {
    await checkVideoMask(page);
    await page.locator('#token').fill(token);
    await checkVideoMask(page);
    await page.locator('#signin button').click();
    await page.waitForFunction(expected => document.querySelector('#identity-name')?.textContent.includes(expected)
      && document.querySelectorAll('#stock tr').length === 3, label, { timeout: 20000 });
  }
  const isResponse = (path) => response => new URL(response.url()).origin === origin &&
    new URL(response.url()).pathname === path && response.request().method() === 'POST';

  evidence.stage = 'preflight';
  const health = await fetch(origin + '/healthz', { redirect: 'error' }).then(r => r.json());
  requireCheck(health.alive === true && health.model_configured === true, 'configured_model_and_app');
  const before = await api('GET', '/api/drafts', PF_BUYER_TOKEN);
  requireCheck(before.status === 200 && Array.isArray(before.data) && before.data.length === 0, 'empty_synthetic_database');

  videoDir = await privateVideoDirectory();
  browser = await chromium.launch({ headless: true });
  context = await browser.newContext(videoDir
    ? { viewport: { width: 1440, height: 900 }, recordVideo: { dir: videoDir, size: { width: 1440, height: 900 } } }
    : {});
  page = await context.newPage();
  video = videoDir ? page.video() : null;
  if (videoDir && !video) throw new Error('video_initialization_failed');
  page.setDefaultTimeout(20000);
  await page.goto(origin, { waitUntil: 'domcontentloaded' });
  if (videoDir) await prepareVideoPage(page);
  await videoMoment(page, '#signin');
  await signIn(page, PF_BUYER_TOKEN, 'demo-buyer');
  await page.locator('#agent-panel').waitFor({ state: 'visible' });

  // Capture what the actual model returned, even if it did not call a tool or
  // answered incorrectly. A trace event supports tool execution, not semantic accuracy.
  evidence.stage = 'model_query';
  let modelOk = false;
  const modelPrompt = videoDir
    ? '查询安装支架 BRACKET 当前库存和补货缺口。'
    : '查询全部物料当前库存和缺口，请先使用库存查询工具，再回答。';
  evidence.model = { prompt: modelPrompt, prompt_source: 'entered_in_browser' };
  const modelUiStarted = performance.now();
  try {
    await page.locator('#prompt').fill(modelPrompt);
    await videoMoment(page, '#agent-panel');
    const [modelResponse] = await Promise.all([
      page.waitForResponse(isResponse('/api/agent/run'), { timeout: 300000 }),
      page.locator('#agent-form button[type="submit"]').click(),
    ]);
    evidence.model = { ...evidence.model, http_status: modelResponse.status(),
      ui_to_response_elapsed_ms: Math.round(performance.now() - modelUiStarted) };
    // The ID comes from the request actually associated with this response,
    // including an HTTP 504; never substitute a response field or fabricate one.
    const requestBody = modelResponse.request().postDataJSON();
    evidence.model.prompt = typeof requestBody?.prompt === 'string' ? safeAnswer(requestBody.prompt) : null;
    evidence.model.prompt_source = 'actual_request';
    if (requestBody?.prompt !== modelPrompt) {
      evidence.model.observation = 'unexpected_or_missing_prompt';
      throw new Error('unexpected_or_missing_prompt');
    }
    const requestId = requestBody.request_id;
    if (typeof requestId !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(requestId)) {
      evidence.model.observation = 'invalid_or_missing_request_id';
      throw new Error('invalid_agent_request_id');
    }
    evidence.model.request_id = requestId.toLowerCase();
    if (modelResponse.status() === 200) {
      const result = await modelResponse.json();
      const events = Array.isArray(result.trace?.events) ? result.trace.events : [];
      evidence.model = {
        ...evidence.model, response_request_id_matches: typeof result.request_id === 'string' &&
          result.request_id.toLowerCase() === evidence.model.request_id, model: safeAnswer(result.trace?.model),
        model_calls: result.trace?.model_calls, elapsed_ms: result.trace?.elapsed_ms,
        input_tokens: result.trace?.input_tokens, output_tokens: result.trace?.output_tokens,
        answer: safeAnswer(result.answer),
        tool_events: events.map(e => ({ step: e.step,
          tool: ['list_stock', 'list_quotes', 'list_recommendations'].includes(e.tool) ? e.tool : '[other]',
          outcome: e.outcome, elapsed_ms: e.elapsed_ms, result_count: e.result_count })),
        draft_id_is_null: result.draft_id === null,
      };
      await page.locator('#agent-result').waitFor({ state: 'visible', timeout: 20000 });
      modelOk = evidence.model.response_request_id_matches && Number.isInteger(result.trace?.model_calls) && result.trace.model_calls > 0 && result.draft_id === null &&
        events.some(e => ['list_stock', 'list_quotes', 'list_recommendations'].includes(e.tool) && e.outcome === 'ok');
    } else {
      evidence.model.observation = modelResponse.status() === 504
        ? 'http_504; specific connection or inference cause unknown'
        : 'http_error; specific cause unknown';
      await page.waitForFunction(() => document.querySelector('#notice')?.classList.contains('error'), null, { timeout: 20000 });
    }
  } catch {
    evidence.model = { ...(evidence.model || {}),
      observation: evidence.model?.observation || 'browser_observation_failed_or_timed_out; cause unknown' };
  } finally {
    evidence.model = { ...(evidence.model || {}), ui_elapsed_ms: Math.round(performance.now() - modelUiStarted) };
    if (videoDir) evidence.model.timing_note = 'UI timings include deliberate recording pauses; they are not pure inference time.';
  }
  if (videoDir && modelOk) {
    await videoMoment(page, '#agent-result-title', 2200);
    // Keep the original, unedited response visible in successive viewport
    // positions. The model output itself is never replaced or rewritten.
    const answerHeight = await page.locator('#agent-answer').evaluate(el => el.getBoundingClientRect().height);
    for (let i = 0; i < Math.min(20, Math.ceil(answerHeight / 400)); i++) {
      await page.evaluate(() => window.scrollBy({ top: 400, behavior: 'instant' }));
      await page.waitForTimeout(1200);
    }
    await page.locator('#agent-result details:has(> #agent-trace) > summary').click();
    await videoMoment(page, '#agent-trace', 2200);
  }
  evidence.checks.model_query_called_read_only_tool = modelOk;
  await writeEvidence();
  await page.waitForFunction(() => !document.querySelector('#switch-user').disabled, null, { timeout: 30000 });
  const afterModel = await api('GET', '/api/drafts', PF_BUYER_TOKEN);
  requireCheck(afterModel.status === 200 && afterModel.data.length === 0, 'no_model_draft');

  evidence.stage = 'browser_preview';
  await videoMoment(page, '#recommendations');
  await page.locator('#recommendations input[value="BRACKET"]').check();
  const [previewResponse] = await Promise.all([
    page.waitForResponse(isResponse('/api/drafts/preview')),
    page.locator('#create-draft').click(),
  ]);
  requireCheck(previewResponse.status() === 200, 'preview_api_success');
  const preview = await previewResponse.json();
  await page.locator('#draft-preview').waitFor({ state: 'visible' });
  const previewText = await page.locator('#preview-lines').innerText();
  const previewTotal = await page.locator('#preview-total').innerText();
  requireCheck(preview.skus?.length === 1 && preview.skus[0] === 'BRACKET' && preview.lines?.length === 1 &&
    preview.lines[0].sku === 'BRACKET' && preview.lines[0].supplier_id === 'S-FAST' &&
    preview.lines[0].quantity === 20 && Number(preview.lines[0].unit_price) === 11.5 &&
    Number(preview.total) === 230 && previewText.includes('BRACKET') && previewText.includes('S-FAST') &&
    previewText.includes('20') && previewText.includes('11.50') && previewTotal.includes('230.00'), 'preview_display_matches_synthetic_business_values');
  const beforeConfirmation = await api('GET', '/api/drafts', PF_BUYER_TOKEN);
  requireCheck(beforeConfirmation.status === 200 && beforeConfirmation.data.length === 0, 'no_draft_before_explicit_confirmation');
  await videoMoment(page, '#draft-preview', 2500);

  evidence.stage = 'browser_confirmation';
  const [draftResponse] = await Promise.all([
    page.waitForResponse(isResponse('/api/drafts')),
    page.locator('#confirm-draft').click(),
  ]);
  requireCheck(draftResponse.status() === 200, 'confirmed_draft_api_success');
  const draft = await draftResponse.json();
  const confirmedRequest = draftResponse.request();
  const confirmationBody = confirmedRequest.postDataJSON();
  const confirmationKey = (await confirmedRequest.allHeaders())['idempotency-key'];
  requireCheck(typeof confirmationKey === 'string' && confirmationBody.preview_hash === preview.preview_hash &&
    JSON.stringify(confirmationBody.skus) === JSON.stringify(preview.skus) &&
    JSON.stringify(draft.lines) === JSON.stringify(preview.lines) && Number(draft.total) === Number(preview.total) &&
    draft.status === 'draft' && draft.buyer_id === 'demo-buyer', 'draft_matches_confirmed_preview');
  await page.waitForFunction(() => document.querySelector('#notice')?.textContent.includes('已就绪'));
  requireCheck((await page.locator('#drafts').innerText()).includes('BRACKET'), 'draft_visible_in_browser');
  await videoMoment(page, '#drafts', 1800);

  // Retry the exact confirmed HTTP request; only the request ID and preview
  // capability remain in memory, never in the exported evidence.
  const replay = await api('POST', '/api/drafts', PF_BUYER_TOKEN, confirmationBody, confirmationKey);
  const afterReplay = await api('GET', '/api/drafts', PF_BUYER_TOKEN);
  requireCheck(replay.status === 200 && replay.data.draft_id === draft.draft_id && afterReplay.status === 200 &&
    afterReplay.data.length === 1 && afterReplay.data[0].draft_id === draft.draft_id, 'draft_retry_is_idempotent');

  evidence.stage = 'browser_approval_and_erp';
  await videoMoment(page, '#switch-user');
  await page.locator('#switch-user').click();
  await videoMoment(page, '#signin');
  await signIn(page, PF_APPROVER_TOKEN, 'demo-approver');
  const card = page.locator('#drafts article').filter({ hasText: draft.draft_id.slice(0, 8) });
  if (videoDir) {
    await card.scrollIntoViewIfNeeded();
    await videoMoment(page, null);
  }
  const [decisionResponse] = await Promise.all([
    page.waitForResponse(isResponse(`/api/drafts/${draft.draft_id}/decision`)),
    card.getByRole('button', { name: '审批通过' }).click(),
  ]);
  requireCheck(decisionResponse.status() === 200 && (await decisionResponse.json()).status === 'approved', 'approver_approved');
  if (videoDir) await videoMoment(page, '#drafts');
  const [submitResponse] = await Promise.all([
    page.waitForResponse(isResponse(`/api/drafts/${draft.draft_id}/submit`)),
    card.getByRole('button', { name: '提交至模拟 ERP' }).click(),
  ]);
  requireCheck(submitResponse.status() === 200, 'erp_submit_api_success');
  const submitted = await submitResponse.json();
  const erpReference = submitted.erp_reference;
  requireCheck(submitted.status === 'submitted' && typeof erpReference === 'string' && erpReference.length > 0 &&
    submitted.events?.map(e => e.event).join(',') === 'created,approved,submitted' &&
    submitted.events?.map(e => e.actor_id).join(',') === 'demo-buyer,demo-approver,demo-approver', 'receipt_and_audit_returned');
  await card.locator('.receipt strong').waitFor({ state: 'visible' });
  requireCheck((await card.locator('.receipt strong').innerText()) === erpReference &&
    (await card.locator('.badge').innerText()) === '已提交', 'receipt_visible_in_browser');
  if (videoDir) {
    await card.locator('summary').click();
    await videoMoment(page, '#drafts', 2500);
  }
  const retrySubmit = await api('POST', `/api/drafts/${draft.draft_id}/submit`, PF_APPROVER_TOKEN);
  requireCheck(retrySubmit.status === 200 && retrySubmit.data.erp_reference === erpReference &&
    retrySubmit.data.events?.map(e => e.event).join(',') === 'created,approved,submitted', 'erp_submit_retry_is_idempotent');

  evidence.draft = { draft_id: draft.draft_id, sku: 'BRACKET', supplier_id: 'S-FAST', quantity: 20,
    unit_price: '11.50', total: '230.00', status: 'submitted', erp_reference: erpReference,
    events: ['created', 'approved', 'submitted'] };
  evidence.stage = 'complete';
  evidence.status = modelOk ? 'pass' : 'failed';
  if (!modelOk) evidence.failure = evidence.model?.http_status === 504
    ? 'model_query_http_504' : 'model_query_missing_successful_read_only_tool';
} catch (error) {
  evidence.status = 'failed';
  // Only locally defined check names are exported, never exception messages from
  // Playwright, HTTP, model text, headers, or the environment.
  const known = String(error?.message || '');
  const allowed = new Set(['missing_configuration', 'invalid_origin', 'answer_exceeds_evidence_limit',
    'configured_model_and_app', 'empty_synthetic_database', 'no_model_draft', 'preview_api_success',
    'preview_display_matches_synthetic_business_values', 'no_draft_before_explicit_confirmation',
    'confirmed_draft_api_success', 'draft_matches_confirmed_preview', 'draft_visible_in_browser',
    'draft_retry_is_idempotent', 'approver_approved', 'erp_submit_api_success',
    'receipt_and_audit_returned', 'receipt_visible_in_browser', 'erp_submit_retry_is_idempotent']);
  evidence.failure = allowed.has(known) ? known : 'unexpected_or_timeout';
} finally {
  // Playwright finalizes the unedited WebM only after its context closes.
  // The fixed filename is discoverable by the harness without ever printing
  // the private path. A requested recording failure makes the experiment fail.
  try {
    if (context) await context.close();
    if (videoDir) {
      if (!video) throw new Error('video_missing');
      const source = await video.path();
      await copyFile(source, join(videoDir, 'recording.webm'), constants.COPYFILE_EXCL);
      await unlink(source);
      evidence.video = { recorded: true, private_only: true, edited: false,
        pacing: 'Deliberate presentation pauses are part of the continuous recording.' };
    }
  } catch {
    evidence.status = 'failed';
    evidence.failure = 'video_finalization_failed';
  }
  if (browser) await browser.close();
  await writeEvidence();
}
console.log(`Hosted browser experiment: ${evidence.status}; stage: ${evidence.stage}`);
if (evidence.status !== 'pass') process.exitCode = 1;
