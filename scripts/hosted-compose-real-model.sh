#!/usr/bin/env bash
# Synthetic data only. Runs on an ephemeral, GitHub-hosted Ubuntu runner.
set -Eeuo pipefail
set +x
umask 077
: "${RUNNER_TEMP:?}" "${GITHUB_RUN_ID:?}"

root=$(git rev-parse --show-toplevel)
cd "$root"
private=$(mktemp -d "$RUNNER_TEMP/pf-private.XXXXXXXX")
export PF_EVIDENCE_DIR="$private/browser-evidence"
public="$RUNNER_TEMP/pf-public-evidence"
mkdir -p "$PF_EVIDENCE_DIR" "$public"
project="pfhosted${GITHUB_RUN_ID}"
[[ "$project" =~ ^pfhosted[0-9]+$ ]] || exit 2
export COMPOSE_PROJECT_NAME="$project"
export PF_BASE_URL="http://127.0.0.1:8000"
if [[ -n ${PF_VIDEO_PUBLIC_KEY_B64:-} ]]; then
  # The browser writes unencrypted frames only under this private temporary
  # directory. The raw file is never included in the artifact upload path.
  export PF_PRIVATE_VIDEO_DIR="$private/video"
  export PF_VIDEO_RUN_ID="$GITHUB_RUN_ID"
  export PF_VIDEO_SOURCE_COMMIT="$(git rev-parse HEAD)"
  mkdir -m 700 "$PF_PRIVATE_VIDEO_DIR"
fi
phase=initialization
compose_configured=0
event() { printf '{"phase":"%s","result":"%s"}\n' "$1" "$2" >> "$public/events.jsonl"; }
cleanup() {
  local rc=$? cleanup_rc=0 image matches remaining_containers remaining_volumes remaining_networks
  trap - EXIT INT TERM HUP
  if (( rc != 0 )); then event "$phase" failed; fi
  if (( compose_configured )); then
    docker compose down --volumes --remove-orphans > "$private/down.log" 2>&1 || cleanup_rc=1
    for image in "${project}-app" "${project}-erp" "${project}-model"; do
      # `image inspect` cannot distinguish an absent tag from a failed daemon.
      if matches=$(docker image ls -q --filter "reference=$image" 2>"$private/image-list.log"); then
        if [[ -n "$matches" ]]; then docker image rm "$image" > /dev/null 2>&1 || cleanup_rc=1; fi
      else
        cleanup_rc=1
      fi
      if matches=$(docker image ls -q --filter "reference=$image" 2>"$private/image-list.log"); then
        [[ -z "$matches" ]] || cleanup_rc=1
      else
        cleanup_rc=1
      fi
    done
    if remaining_containers=$(docker ps -aq --filter "label=com.docker.compose.project=$project" 2>"$private/container-list.log"); then
      [[ -z "$remaining_containers" ]] || cleanup_rc=1
    else cleanup_rc=1; fi
    if remaining_volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project" 2>"$private/volume-list.log"); then
      [[ -z "$remaining_volumes" ]] || cleanup_rc=1
    else cleanup_rc=1; fi
    if remaining_networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project" 2>"$private/network-list.log"); then
      [[ -z "$remaining_networks" ]] || cleanup_rc=1
    else cleanup_rc=1; fi
    if (( cleanup_rc == 0 )); then
      event cleanup compose_containers_volumes_networks_and_project_image_tags_removed
    else
      event cleanup failed_or_unconfirmed
      rc=1
    fi
  else
    event cleanup compose_not_started
  fi
  # Private logs, credentials and certificates are never included in the artifact.
  case "$private" in "$RUNNER_TEMP"/pf-private.*) rm -rf -- "$private";; *) event cleanup unsafe_private_path; rc=1;; esac
  exit "$rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

python3 - "$public/provenance.json" "$(git rev-parse HEAD)" <<'PY'
import json,sys
data={'source_commit':sys.argv[2], 'environment':'GitHub-hosted Ubuntu; Docker Compose; synthetic data only',
 'model':'Qwen/Qwen2.5-0.5B-Instruct-GGUF q4_k_m',
 'model_revision':'9217f5db79a29953eb74d5343926648285ec7e67',
 'model_sha256':'74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db',
 'llama_cpp_release':'b12433',
 'llama_archive_sha256':'209023cfc6061720802c103e5b7ba9f99e9304a3bdcd035fc50c3fba064cb79f',
 'model_timeout_seconds':120,
 'limits':'One synthetic browser scenario; successful execution does not establish model answer accuracy.'}
with open(sys.argv[1],'w',encoding='utf8') as f: json.dump(data,f,indent=2)
PY
event initialization started
phase=resource_check
available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
disk=$(df -Pk "$RUNNER_TEMP" | awk 'NR==2 {print $4}')
[[ "$available" =~ ^[0-9]+$ && "$disk" =~ ^[0-9]+$ ]] && (( available >= 4194304 && disk >= 8388608 ))
event "$phase" passed

phase=download_and_verify
curl -fsSL --retry 2 --connect-timeout 20 --max-time 600 \
  'https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/resolve/9217f5db79a29953eb74d5343926648285ec7e67/qwen2.5-0.5b-instruct-q4_k_m.gguf' \
  -o "$private/model.gguf"
curl -fsSL --retry 2 --connect-timeout 20 --max-time 300 \
  'https://github.com/ggml-org/llama.cpp/releases/download/b12433/llama-b12433-bin-ubuntu-x64.tar.gz' \
  -o "$private/llama.tar.gz"
printf '%s  %s\n' 74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db "$private/model.gguf" | sha256sum -c --status
printf '%s  %s\n' 209023cfc6061720802c103e5b7ba9f99e9304a3bdcd035fc50c3fba064cb79f "$private/llama.tar.gz" | sha256sum -c --status
event "$phase" passed

phase=prepare
mkdir "$private/model-build" "$private/tls"
tar -xzf "$private/llama.tar.gz" -C "$private/model-build"
cat > "$private/model-build/Dockerfile" <<'DOCKER'
FROM ubuntu:24.04
RUN apt-get update && apt-get install -y --no-install-recommends libssl3t64 libgomp1 ca-certificates && rm -rf /var/lib/apt/lists/*
COPY llama-b12433/ /opt/llama/
ENV LD_LIBRARY_PATH=/opt/llama
ENTRYPOINT ["/opt/llama/llama-server"]
DOCKER
python3 - "$private/environment" "$private/tls/api-key" <<'PY'
import secrets,sys
names=('MYSQL_PASSWORD','PROCUREFLOW_BUYER_TOKEN','PROCUREFLOW_APPROVER_TOKEN','PROCUREFLOW_ERP_TOKEN','PROCUREFLOW_MODEL_API_KEY')
values={name:secrets.token_hex(24) for name in names}
with open(sys.argv[1],'x',encoding='ascii') as f:
 for name,value in values.items(): f.write(f'{name}={value}\n')
 f.write('PROCUREFLOW_MODEL_URL=https://model:9443/v1\nPROCUREFLOW_MODEL_NAME=qwen2.5-0.5b-instruct-q4_k_m\nPROCUREFLOW_MODEL_TIMEOUT_SECONDS=120\n')
with open(sys.argv[2],'x',encoding='ascii') as f: f.write(values['PROCUREFLOW_MODEL_API_KEY'])
PY
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=ProcureFlow synthetic CA' -addext 'basicConstraints=critical,CA:TRUE' -keyout "$private/tls/ca.key" -out "$private/tls/ca.pem" > "$private/cert.log" 2>&1
openssl req -newkey rsa:2048 -nodes -subj '/CN=model' -keyout "$private/tls/model.key" -out "$private/tls/model.csr" >> "$private/cert.log" 2>&1
printf 'subjectAltName=DNS:model\nextendedKeyUsage=serverAuth\nbasicConstraints=critical,CA:FALSE\n' > "$private/tls/extensions"
openssl x509 -req -days 1 -in "$private/tls/model.csr" -CA "$private/tls/ca.pem" -CAkey "$private/tls/ca.key" -CAcreateserial -extfile "$private/tls/extensions" -out "$private/tls/model.pem" >> "$private/cert.log" 2>&1
touch "$private/tls/trust.pem"
chmod 644 "$private/tls/trust.pem"
cat > "$private/override.yaml" <<YAML
services:
  model:
    image: ${project}-model
    command: ["--offline", "--model", "/model/model.gguf", "--host", "0.0.0.0", "--port", "9443", "--n-gpu-layers", "0", "--ctx-size", "2048", "--threads", "2", "--threads-batch", "2", "--parallel", "1", "--no-warmup", "--jinja", "--ssl-key-file", "/tls/model.key", "--ssl-cert-file", "/tls/model.pem", "--api-key-file", "/tls/api-key"]
    volumes:
      - ${private}/model.gguf:/model/model.gguf:ro
      - ${private}/tls/model.key:/tls/model.key:ro
      - ${private}/tls/model.pem:/tls/model.pem:ro
      - ${private}/tls/api-key:/tls/api-key:ro
  app:
    volumes:
      - ${private}/tls/trust.pem:/app/.venv/lib/python3.12/site-packages/certifi/cacert.pem:ro
YAML
export COMPOSE_FILE="$root/compose.yaml:$private/override.yaml"
export COMPOSE_ENV_FILES="$private/environment"
compose_configured=1
event "$phase" passed

phase=build
docker build -t "${project}-model" "$private/model-build" > "$private/model-build.log" 2>&1
docker compose build > "$private/compose-build.log" 2>&1
docker run --rm --entrypoint /app/.venv/bin/python "${project}-app" -c 'import certifi; print(open(certifi.where(),encoding="ascii").read())' > "$private/tls/trust.pem"
cat "$private/tls/ca.pem" >> "$private/tls/trust.pem"
chmod 644 "$private/tls/trust.pem"
event "$phase" passed

phase=start
docker compose up -d > "$private/up.log" 2>&1
for image in "${project}-model" "${project}-app" mysql:8.4; do
  printf '%s %s\n' "$image" "$(docker image inspect "$image" --format '{{.Id}}')" >> "$public/image-ids.txt"
done
python3 - <<'PY'
import json,time,urllib.request
for _ in range(100):
 try:
  with urllib.request.urlopen('http://127.0.0.1:8000/readyz',timeout=2) as r:
   if json.load(r).get('ready') is True: break
 except Exception: pass
 time.sleep(2)
else: raise SystemExit('Application did not become ready')
PY
event "$phase" passed

phase=tls_and_authentication
docker compose exec -T app /app/.venv/bin/python - <<'PY'
import os,socket,ssl,time,httpx
url='https://model:9443/v1/models'; token=os.environ['PROCUREFLOW_MODEL_API_KEY']
def cert_error(error,codes):
 seen=set(); todo=[error]
 while todo:
  e=todo.pop()
  if e is None or id(e) in seen: continue
  seen.add(id(e))
  if isinstance(e,ssl.SSLCertVerificationError) and e.verify_code in codes: return True
  todo.extend((e.__cause__,e.__context__))
 return False
with httpx.Client(trust_env=False,timeout=10) as c:
 for _ in range(90):
  try:
   if c.get(url,headers={'Authorization':'Bearer '+token}).status_code==200: break
  except httpx.HTTPError: pass
  time.sleep(2)
 else: raise SystemExit('Authenticated HTTPS model did not become ready')
 if c.get(url).status_code != 401: raise SystemExit('Unauthenticated model request accepted')
 try: c.get('https://'+socket.gethostbyname('model')+':9443/v1/models',headers={'Authorization':'Bearer '+token})
 except httpx.ConnectError as e:
  if not cert_error(e,{62,64}): raise SystemExit('Wrong-host test failed for another reason')
 else: raise SystemExit('Wrong hostname accepted')
try:
 with httpx.Client(verify=ssl.create_default_context(),trust_env=False,timeout=5) as c: c.get(url,headers={'Authorization':'Bearer '+token})
except httpx.ConnectError as e:
 if not cert_error(e,{18,19,20,21}): raise SystemExit('Untrusted-CA test failed for another reason')
else: raise SystemExit('Untrusted CA accepted')
PY
event "$phase" passed

if [[ -n ${PF_PRIVATE_VIDEO_DIR:-} ]]; then
  phase=video_public_key_validation
  # Validate the public key before allowing the browser to capture anything.
  # The locked application container already includes cryptography. No
  # private key or browser content enters the environment or logs.
  docker compose exec -T \
    -e "PF_VIDEO_PUBLIC_KEY_B64=$PF_VIDEO_PUBLIC_KEY_B64" \
    -e "PF_VIDEO_RUN_ID=$PF_VIDEO_RUN_ID" \
    -e "PF_VIDEO_SOURCE_COMMIT=$PF_VIDEO_SOURCE_COMMIT" \
    app /app/.venv/bin/python -c "$(cat "$root/scripts/video_envelope.py")" validate-key \
    > "$private/video-key-check.log" 2>&1
  event "$phase" passed
fi

phase=browser_acceptance
# The test receives only one-run demo credentials; stdout/stderr remain private.
set -a
source "$private/environment"
set +a
export PF_BUYER_TOKEN="$PROCUREFLOW_BUYER_TOKEN" PF_APPROVER_TOKEN="$PROCUREFLOW_APPROVER_TOKEN"
browser_rc=0
node tests/hosted_browser_acceptance.mjs > "$private/browser.log" 2>&1 || browser_rc=$?
if (( browser_rc == 0 )); then event "$phase" passed; else event "$phase" failed; fi

phase=agent_run_diagnostics
agent_trace_rc=0
# Query only trace metadata from the synthetic database. Project a strict
# allowlist before anything leaves the container; never retrieve prompts,
# answers, headers, model logs or credential values. The browser's request ID
# binds this observation to the exact request rather than another attempt.
request_id=''
if request_id=$(python3 - "$PF_EVIDENCE_DIR/browser-acceptance.json" 2>/dev/null <<'PY'
import json,sys
from uuid import UUID
data=json.load(open(sys.argv[1],encoding='utf8'))
value=data.get('model',{}).get('request_id')
if not isinstance(value,str) or str(UUID(value)) != value: raise ValueError('invalid request id')
print(value)
PY
); then
docker compose exec -T app /app/.venv/bin/python - "$request_id" > "$private/agent-runs.json" 2>/dev/null <<'PY' || agent_trace_rc=$?
import json,os,sys
from uuid import UUID
from sqlalchemy import create_engine,text
request_id=sys.argv[1]
if str(UUID(request_id)) != request_id: raise ValueError('invalid request id')
allowed_tools={'list_stock','list_quotes','list_recommendations'}
def number(value):
 return value if type(value) is int and 0 <= value <= 2147483647 else None
def safe_event(event):
 if not isinstance(event,dict) or event.get('tool') not in allowed_tools: return None
 return {'step':number(event.get('step')),'tool':event['tool'],
         'outcome':event.get('outcome') if event.get('outcome') in ('ok','rejected') else 'unknown',
         'elapsed_ms':number(event.get('elapsed_ms')),'result_count':number(event.get('result_count'))}
engine=create_engine(os.environ['PROCUREFLOW_DATABASE_URL'])
with engine.connect() as connection:
 rows=connection.execute(text('SELECT status,trace FROM agent_runs WHERE request_id = :request_id ORDER BY attempt_id LIMIT 2'),
                         {'request_id':request_id}).all()
if len(rows) != 1: raise ValueError('expected exactly one matching run')
runs=[]
for status,trace in rows:
 if isinstance(trace,str): trace=json.loads(trace)
 if not isinstance(trace,dict): raise ValueError('invalid trace')
 raw_events=trace.get('events',[])
 if not isinstance(raw_events,list): raise ValueError('invalid events')
 events=[item for item in (safe_event(e) for e in raw_events[:100]) if item is not None]
 runs.append({'status':status if status in ('completed','failed','replayed') else 'unknown',
              'model_calls':number(trace.get('model_calls')),'elapsed_ms':number(trace.get('elapsed_ms')),
              'input_tokens':number(trace.get('input_tokens')),'output_tokens':number(trace.get('output_tokens')),
              'events':events})
print(json.dumps({'request_id':request_id,'runs':runs},separators=(',',':')))
PY
else
  agent_trace_rc=1
fi
if (( agent_trace_rc == 0 )) && python3 -m json.tool "$private/agent-runs.json" > /dev/null; then
  cp "$private/agent-runs.json" "$public/agent-runs.json"
  event "$phase" collected
else
  agent_trace_rc=1
  event "$phase" failed_or_unavailable
fi

phase=agent_browser_crosscheck
agent_compare_rc=1
if (( agent_trace_rc == 0 )); then
  comparison=''
  if comparison=$(python3 - "$PF_EVIDENCE_DIR/browser-acceptance.json" "$private/agent-runs.json" 2>/dev/null <<'PY'
import json,sys
browser=json.load(open(sys.argv[1],encoding='utf8'))
diagnostic=json.load(open(sys.argv[2],encoding='utf8'))
if browser.get('checks',{}).get('model_query_called_read_only_tool') is not True:
 print('model_not_successful')
else:
 model=browser.get('model',{})
 runs=diagnostic.get('runs',[])
 if len(runs)!=1 or diagnostic.get('request_id')!=model.get('request_id'): raise ValueError('run mismatch')
 run=runs[0]
 if model.get('http_status')!=200 or model.get('response_request_id_matches') is not True or run.get('status')!='completed':
  raise ValueError('model status mismatch')
 calls=model.get('model_calls')
 if type(calls) is not int or calls < 1 or type(run.get('model_calls')) is not int or calls!=run['model_calls']:
  raise ValueError('model call count mismatch')
 fields=('step','tool','outcome','elapsed_ms','result_count')
 allowed={'list_stock','list_quotes','list_recommendations'}
 def successful(event):
  if not isinstance(event,dict) or event.get('tool') not in allowed or event.get('outcome')!='ok': return None
  if any(field not in event for field in fields): return None
  if type(event['step']) is not int or type(event['elapsed_ms']) is not int: return None
  if event['result_count'] is not None and type(event['result_count']) is not int: return None
  return tuple(event[field] for field in fields)
 browser_events=model.get('tool_events',[])
 database_events=run.get('events',[])
 if not isinstance(browser_events,list) or not isinstance(database_events,list): raise ValueError('invalid events')
 b={item for item in (successful(e) for e in browser_events) if item is not None}
 d={item for item in (successful(e) for e in database_events) if item is not None}
 if not b.intersection(d): raise ValueError('no matching successful tool event')
 print('passed')
PY
  ); then
    if [[ "$comparison" == passed ]]; then
      agent_compare_rc=0
      event "$phase" passed
    elif [[ "$comparison" == model_not_successful ]]; then
      event "$phase" not_applicable_model_not_successful
    else
      event "$phase" failed_or_unavailable
    fi
  else
    event "$phase" failed_or_unavailable
  fi
else
  event "$phase" failed_or_unavailable
fi

phase=browser_evidence
# Do not publish arbitrary browser logs or screenshots. Accept only JSON without
# any generated token, authorization header or key material.
python3 - "$PF_EVIDENCE_DIR" "$public" "$private/environment" <<'PY'
import json,pathlib,re,sys
src,out,env=map(pathlib.Path,sys.argv[1:])
secrets=[line.split('=',1)[1] for line in env.read_text().splitlines() if '=' in line and ('TOKEN=' in line or 'PASSWORD=' in line or 'API_KEY=' in line)]
files=list(src.glob('*.json'))
if not files: raise SystemExit('Browser did not create JSON evidence')
for f in files:
 if f.stat().st_size>200000: raise SystemExit('Browser evidence exceeds size limit')
 raw=f.read_text(encoding='utf8'); data=json.loads(raw)
 if any(secret and secret in raw for secret in secrets) or re.search(r'(?i)bearer\s+\S+|-----BEGIN .*PRIVATE KEY-----',raw):
  raise SystemExit('Browser evidence contains credential material')
 (out/f.name).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8')
PY
event "$phase" passed

# Compare the actual application database and independent ERP store to the
# browser's confirmed draft, then restart the app and verify it is still visible.
phase=database_and_erp_crosscheck
draft_id=$(python3 - "$PF_EVIDENCE_DIR/browser-acceptance.json" <<'PY'
import json,re,sys
d=json.load(open(sys.argv[1],encoding='utf8')).get('draft',{}).get('draft_id','')
if not re.fullmatch(r'[0-9a-fA-F-]{36}',d): raise SystemExit('Browser did not complete a draft')
print(d)
PY
)
docker compose exec -T app /app/.venv/bin/python - "$draft_id" > "$private/mysql.json" <<'PY'
import json,os,sys
from sqlalchemy import create_engine,text
e=create_engine(os.environ['PROCUREFLOW_DATABASE_URL'])
with e.connect() as c:
 # MySQL reserves LINES; this cross-check runs against the Compose MySQL service.
 rows=c.execute(text('SELECT draft_id,buyer_id,status,`lines`,total,erp_reference FROM purchase_drafts')).all()
 events=c.execute(text('SELECT event,actor_id FROM purchase_draft_events WHERE draft_id=:id ORDER BY event_id'),{'id':sys.argv[1]}).all()
assert len(rows)==1
row=rows[0]; assert row.draft_id==sys.argv[1] and row.buyer_id=='demo-buyer' and row.status=='submitted'
assert events==[('created','demo-buyer'),('approved','demo-approver'),('submitted','demo-approver')]
lines=json.loads(row.lines) if isinstance(row.lines,str) else row.lines
print(json.dumps({'draft_id':row.draft_id,'lines':lines,'total':row.total,'reference':row.erp_reference,'event_count':len(events)}))
PY
docker compose exec -T erp /app/.venv/bin/python - "$draft_id" > "$private/erp.json" <<'PY'
import json,sqlite3,sys
with sqlite3.connect('/data/erp.sqlite3') as c:
 rows=c.execute('SELECT idempotency_key,payload,reference FROM erp_receipts').fetchall()
assert len(rows)==1 and rows[0][0]==sys.argv[1]
print(json.dumps({'draft_id':rows[0][0],'lines':json.loads(rows[0][1]),'reference':rows[0][2],'receipt_count':len(rows)}))
PY
python3 - "$PF_EVIDENCE_DIR/browser-acceptance.json" "$private/mysql.json" "$private/erp.json" "$public/storage-crosscheck.json" "$public/provenance.json" "$root/scripts/hosted-compose-real-model.sh" <<'PY'
import hashlib,json,re,sys
from datetime import date
from decimal import Decimal
from pathlib import Path
browser,mysql,erp=(json.load(open(p,encoding='utf8')) for p in sys.argv[1:4]); d=browser['draft']
provenance=json.load(open(sys.argv[5],encoding='utf8')); source_commit=provenance['source_commit']
assert isinstance(source_commit,str) and re.fullmatch(r'[0-9a-f]{40}',source_commit)
assert d['draft_id']==mysql['draft_id']==erp['draft_id'] and d['erp_reference']==mysql['reference']==erp['reference']
fields={'sku','supplier_id','quantity','unit_price','due_date'}
def canonical_line(lines):
 assert isinstance(lines,list) and len(lines)==1
 line=lines[0]
 # Fail closed on missing or new fields rather than silently treating a
 # partial comparison as equality of complete purchase lines.
 assert isinstance(line,dict) and set(line)==fields
 assert isinstance(line['sku'],str) and isinstance(line['supplier_id'],str)
 assert type(line['quantity']) is int and line['quantity']>0
 assert isinstance(line['unit_price'],str)
 price=Decimal(line['unit_price']); assert price.is_finite() and price>0
 due=line['due_date']; assert isinstance(due,str) and date.fromisoformat(due).isoformat()==due
 return (line['sku'],line['supplier_id'],line['quantity'],price,due)
mysql_line=canonical_line(mysql['lines']); erp_line=canonical_line(erp['lines'])
assert mysql_line==erp_line
assert mysql_line[:4]==(d['sku'],d['supplier_id'],d['quantity'],Decimal(str(d['unit_price'])))
assert mysql_line[:4]==('BRACKET','S-FAST',20,Decimal('11.50'))
assert Decimal(str(d['total']))==Decimal(str(mysql['total']))==mysql_line[2]*mysql_line[3]==Decimal('230.00')
json.dump({'draft_id':d['draft_id'],'receipt':d['erp_reference'],'mysql_draft_count':1,'mysql_event_count':mysql['event_count'],
 'erp_receipt_count':erp['receipt_count'],'source_commit':source_commit,
 'script_sha256':hashlib.sha256(Path(sys.argv[6]).read_bytes()).hexdigest(),
 'draft_id_and_receipt_match':True,'mysql_erp_full_line_fields_compared':sorted(fields),
 'mysql_erp_full_line_match':True,'browser_summary_line_fields_compared':['sku','supplier_id','quantity','unit_price'],
 'browser_due_date_compared':False,'browser_summary_mysql_total_matches_line_calculation':True},
 open(sys.argv[4],'w',encoding='utf8'),indent=2)
PY
event "$phase" passed

phase=application_restart
docker compose restart app > "$private/restart.log" 2>&1
python3 - "$draft_id" "$PF_EVIDENCE_DIR/browser-acceptance.json" <<'PY'
import json,os,sys,time,urllib.request
expected=json.load(open(sys.argv[2],encoding='utf8'))['draft']['erp_reference']
request=urllib.request.Request('http://127.0.0.1:8000/api/drafts',headers={'Authorization':'Bearer '+os.environ['PF_BUYER_TOKEN']})
for _ in range(60):
 try:
  with urllib.request.urlopen(request,timeout=3) as r: data=json.load(r)
  if len(data)==1 and data[0]['draft_id']==sys.argv[1] and data[0]['status']=='submitted' and data[0]['erp_reference']==expected: break
 except Exception: pass
 time.sleep(2)
else: raise SystemExit('Draft not recovered after application restart')
PY
event "$phase" passed
if (( browser_rc != 0 || agent_trace_rc != 0 || agent_compare_rc != 0 )); then phase=browser_model_or_business_checks; exit 1; fi
if [[ -n ${PF_PRIVATE_VIDEO_DIR:-} ]]; then
  phase=video_encryption
  # Only after every experiment check and browser closure succeeded do we seal
  # exactly one raw recording. A partial/corrupt envelope stays private.
  mapfile -d '' recordings < <(find "$PF_PRIVATE_VIDEO_DIR" -maxdepth 1 -type f -name '*.webm' -print0)
  if (( ${#recordings[@]} != 1 )); then
    echo 'Expected one completed private recording.' >&2
    exit 1
  fi
  docker compose exec -T \
    -e "PF_VIDEO_PUBLIC_KEY_B64=$PF_VIDEO_PUBLIC_KEY_B64" \
    -e "PF_VIDEO_RUN_ID=$PF_VIDEO_RUN_ID" \
    -e "PF_VIDEO_SOURCE_COMMIT=$PF_VIDEO_SOURCE_COMMIT" \
    app /app/.venv/bin/python -c "$(cat "$root/scripts/video_envelope.py")" encrypt \
    < "${recordings[0]}" > "$private/video.pfvideo" 2> "$private/video-encryption.log"
  test -s "$private/video.pfvideo"
  # Reject an existing destination: a run never replaces another artifact.
  mkdir -m 700 "$RUNNER_TEMP/pf-encrypted-video"
  mv -- "$private/video.pfvideo" "$RUNNER_TEMP/pf-encrypted-video/video.pfvideo"
  event "$phase" encrypted_for_local_review
fi
event experiment completed
