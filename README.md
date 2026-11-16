# ProcureFlow：采购补货协同助手

给采购员和审批人用的可复现演示。采购员查缺货和供应商报价、生成草案，审批人确认后提交到独立的模拟 ERP，拿到可追踪的回执。库存、价格和供应商均为合成数据。

## 项目故事

**业务问题：**采购员要同时核对现有库存、预留、在途、预测需求和供应商交期，才能判断需要补多少、找谁买。系统把这些数据汇总成可解释的采购建议，并把下单动作留给审批人。

**关键取舍：**模型负责理解请求和调用受控工具，数量、金额和供应商排序由确定性规则计算；模型不能审批或提交。模拟 ERP 用草案编号去重，避免请求超时重试时重复受理。

**实际排障：**一次审查发现建单成功但响应丢失后，如果库存已变化，同一个请求重试可能拿不到原草案。现在按采购员、幂等键和原始请求保存意图，再根据意图返回原单；SQLite 与 MySQL 的并发重试都有测试。接入真实模型时还发现服务一次返回多个工具调用，已增加受限顺序处理和数量上限。首次容器部署还发现严格 `umask 077` 下源码对非 root 用户不可读，已在镜像构建时规范源码权限并重验。

**可展示的证据：**已用 MySQL 8.4 和浏览器完成「查询缺口 → 创建草案 → 审批 → 模拟 ERP 回执」。[真实模型用例记录](evidence/magpie-evaluation.json)包含通过本地 Magpie 调用的 15 条固定场景：自动检查均通过，助手已逐条对照合成数据，尚需独立人工复核。这是经过调试后的一次固定样本测试，不代表生产准确率。数据与 ERP 均为模拟，不能声称对接了真实企业系统，也未测出生产环境的节省工时。

请求重试逻辑更新后进行了[另一轮 15 条真实模型场景](evidence/magpie-evaluation-replay.json)，自动检查均通过；逐条查看时发现一条回答仅凭没有报价就断言物料不存在，这个结论缺少证据。另一条先说“订不到”，随后才说明是无法在七天内交货。结果仍需独立人工复核，自动检查不等于语义正确。

针对前一问题，报价工具补充了物料在当前目录中是否存在的证据。[三条定向真实模型复测](evidence/magpie-quote-grounding.json)分别覆盖“有物料无报价”“目录中无物料”“有物料有报价”；本次回答与合成数据一致，但样本太少，不能推断整体准确率。

报价工具更新后又用当前源码[重跑了完整 15 条场景](evidence/magpie-evaluation-current.json)：自动检查均通过，先前的未知 SKU 回答限定在当前目录。逐条查看仍发现：一处把需求截止日标为交期、随后作了解释；一处已正确建单，但回答未列出供应商、数量和金额。结果仍需独立人工复核。

## 运行

用 Docker Compose 启动应用、MySQL 和模拟 ERP。需要 Python 3.12 运行令牌生成脚本；脚本只创建本地 `.env`，不会覆盖已有配置。

在 POSIX 系统上，新建的 `.env` 只授予文件所有者读写权限；在 Windows 上文件继承所在目录的 ACL，应确保工作目录只允许获授权的本机用户访问。

已在 VMware 实验室用 Docker Compose 完整构建并启动应用、MySQL 和模拟 ERP，验证权限、采购草案重试、审批及 ERP 回执；[实测记录](evidence/compose-smoke.json)包含源码哈希和检查范围。本次容器验收确认了页面 HTML 可访问，但没有在该容器实例上操作浏览器或配置真实模型。[GitHub Actions 运行记录](https://github.com/wanghaibing07/procureflow/actions/workflows/test.yml)可查看每次提交的自动化测试结果。

```bash
python scripts/demo_env.py
docker compose up --build
```

打开 [采购工作台](http://127.0.0.1:8000)，轮流输入 `.env` 里的 `PROCUREFLOW_BUYER_TOKEN` 和 `PROCUREFLOW_APPROVER_TOKEN`。模拟 ERP 和 MySQL 端口不映射到宿主机。

没有 Docker 时，安装 uv 并运行一次令牌生成脚本，然后在两个终端分别启动 HTTP 服务。此路径默认使用两个独立 SQLite 文件：

```bash
uv sync --locked
uv run --locked --env-file .env uvicorn procureflow.mock_erp:app --port 9001
uv run --locked --env-file .env uvicorn procureflow.api:app --port 8000
```

## 演示路径

1. 以采购员身份登录，查看 `2026-01-01` 起未来 7 天的数据。`BRACKET` 缺 20 件，供应商 `S-FAST` 可按时交货，金额为 230.00；`GASKET` 缺 23 件，但没有可按时送达的报价。
2. 勾选 `BRACKET`，生成草案并核对明细。采购员没有审批和提交权限。
3. 切换到审批人，审批通过，再点「提交至模拟 ERP」。检查回执和三条审计事件。通过 API 重试同一采购单会得到同一个回执。

库存和报价规则在 Python 中执行。模拟 ERP 根据采购单 ID 持久去重；如果提交请求超时，审批人可重试同一草案。业务库和模拟 ERP 回执分开存储。

建草案时如果响应中断，页面会在本浏览器保留未确认请求的随机编号；刷新、重新登录后用相同采购员和相同参数重试，将继续使用该编号。浏览器清除站点数据或换设备后无法找回这个编号，此时应先检查已有草案，再决定是否重新建单。

模型辅助建单时，如果服务进程在草案提交前被强制终止，该请求可能一直显示为处理中，暂时不能用同一个编号自动恢复。应先人工核查模型调用确已结束，并检查草案列表；确认没有已有草案后，才发起新的请求。不要仅凭超时认定未建单。若草案已提交，同编号重试会找回原草案。

## 可选模型

要启用自然语言输入，在本地 `.env` 增加 `PROCUREFLOW_MODEL_URL`、`PROCUREFLOW_MODEL_API_KEY`、`PROCUREFLOW_MODEL_NAME`，重启应用。URL 填模型服务的 Chat Completions 基础地址（如以 `/v1` 结尾），模型必须支持函数工具调用。工具仅处理本演示的合成数据。

使用 Compose 时，模型地址必须从应用容器内可达；`127.0.0.1` 指向容器自身，不能直接访问宿主机上的 Magpie。若模型服务只监听宿主机回环地址，需先通过适合当前平台的受控连接方式提供容器可达的地址，再配置该地址。本项目尚未验收 Compose 与宿主机 Magpie 的组合。

模型可以查库存与报价。采购员显式勾选授权后，它才能按 SKU 建草案，报价和数量由服务端计算。审批和提交保留为独立的人工调用。工具调用轨迹可在界面查看；数据库保存模型、步骤、用量等精简记录，不记录提示词和答复全文。

## 验收

在已经启动、只包含合成演示数据的隔离环境中，可以运行以下本机 API 验收。脚本读取本地令牌但不输出令牌；它会创建并提交一张新的演示草案，并验证角色权限、重试、模拟 ERP 回执和审计事件。不要针对真实业务环境运行。

```bash
python scripts/verify_compose.py --base-url http://127.0.0.1:8000 --env-file .env --synthetic-demo
```

```bash
uv run --locked pytest -q
```

默认使用本地 SQLite 和模拟模型输入。要追加 MySQL 验收，设置 `PROCUREFLOW_TEST_DB_URL` 为独立的 `procureflow_test` 数据库连接，再运行 `uv run --locked pytest -q tests/test_api.py tests/test_agent.py`；测试只接受数据库名严格等于 `procureflow_test` 的 MySQL 地址，并使用唯一请求和采购员数据。测试覆盖未审批的提交、采购员越权、同一草案重放、模型请求并发和 ERP 回执。真实模型应另外核对工具调用、草案参数和最终结果；模拟测试不计入真实模型成功率。

如果已安装 ego-browser，可运行下面的浏览器测试；将示例中的文件 URL 换成该测试文件的实际绝对路径。测试使用本地合成的模拟服务，验证建单响应中断后的刷新重试，不需要真实模型或数据库：

```bash
ego-browser nodejs -e "await import('file:///ABSOLUTE/PATH/TO/procureflow/tests/browser_retry.mjs')"
```

同目录的 `browser_retry_real.mjs` 使用项目虚拟环境启动真实 API、独立临时 SQLite 和本机模拟模型，分别模拟手动及模型辅助建单提交后响应丢失，检查刷新重试返回原草案。它不调用真实模型；复跑时按上面的方式将文件名替换为 `browser_retry_real.mjs`。

配置真实模型并启动独立演示环境后，可运行 15 条固定业务场景；此操作会发起模型请求，可能产生费用，仅针对合成数据和本地演示服务。先通过安全的终端环境设置 `PROCUREFLOW_BUYER_TOKEN` 和 `PROCUREFLOW_APPROVER_TOKEN`，然后执行：

```bash
uv run --locked python scripts/evaluate_agent.py --base-url http://127.0.0.1:8000 --output evaluation.json --run-live
```

结果文件不得已存在。脚本检查工具调用、草案内容和未授权写入，并将语义正确性保留为人工复核项；需逐条查看回答和轨迹才能计算任务成功率。
