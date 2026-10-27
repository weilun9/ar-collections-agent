# 应收账款催收 Agent（AR Collections Agent）

PM 方案文档的可运行实现。[`docs/PRD.zh-CN.md`](docs/PRD.zh-CN.md) 里写的每一条设计
——三级决策矩阵、48 小时缓冲期、关键词强制升级、七层风险边界——
在这里都有对应的代码，并且都能当场跑给人看。

**纯 Python 标准库，零第三方依赖。** 演示环境少一个依赖，就少一个当天出错的可能。

---

## 60 秒跑起来

不想敲命令，直接双击这三个文件也行：

| 文件 | 作用 |
|---|---|
| `演示.bat` | 跑完整演示剧本 |
| `面板.bat` | 启动可视化面板并自动开浏览器 |
| `重置并自检.bat` | 重置演示数据 + 跑一遍风险边界自检 |

命令行：

```bash
cd E:\开发项目\ar-collections-agent
```

```bash
python ar.py seed
```

```bash
python ar.py demo
```

`demo` 是 13 幕的完整演示剧本，每一幕先说明"接下来要证明什么"，再执行，再看结果。
按回车推进；想连续跑完加 `--no-pause`。

全程 **dry-run** —— 所有消息写入 `data/outbox/`，不会发给任何真实收件人。

---

## 常用命令

| 命令 | 作用 |
|---|---|
| `python ar.py seed` | 生成 30 笔演示账款（`--today 2026-08-25` 指定锚点日） |
| `python ar.py matrix -v` | 预演分级结果并显示判定依据，**不产生任何动作、不改任何文件** |
| `python ar.py scan` | 执行一次每日扫描（`--date "2026-08-27 10:00"` 可推演未来） |
| `python ar.py outbox` | 看 Agent 实际发出的消息全文 |
| `python ar.py log -v` | 审计日志：谁做的、对谁、为什么 |
| `python ar.py report --send` | 生成并投递 CFO 周报（Markdown + HTML） |
| `python ar.py rules` | 展示 L2/L3/L4 的风险边界设计 |
| `python ar.py verify` | **自检：逐条验证每层防线是否真的生效** |
| `python ar.py dashboard` | 可视化面板 → http://127.0.0.1:8787/ |
| `python ar.py takeover INV-2010 --by 王磊` | 人工接管，Agent 立即停手 |
| `python ar.py reply INV-2005 --text "能不能分三期"` | 模拟客户回复，触发强制升级 |
| `python -m unittest discover -s tests` | 58 项单元测试 |

推演时间线是最好用的演示手法：

```bash
python ar.py scan --date "2026-08-25 09:00"
```

```bash
python ar.py scan --date "2026-08-27 10:00"
```

第二条会看到 48 小时缓冲期到期、客户经理未响应的账款被 Agent 补位发出正式对账函。

---

## 项目结构

```
ar-collections-agent/
├─ ar.py                    入口（python ar.py <命令>）
├─ config.json              所有业务阈值，PM/财务改这里，不用改代码
├─ src/ar_agent/
│  ├─ tiering.py            §4  分级引擎（纯函数：客观事实 → 分级）
│  ├─ policy.py             §5  L4 运行时策略层 ★ 整套设计的核心
│  ├─ inbound.py            §4.2 关键词强制升级（纯规则，不经过 LLM）
│  ├─ templates.py          §5  L2 预设模板 + 发送前内容闸门
│  ├─ store/                §5  L3 IAM 字段白名单（Agent 无权改金额/账期）
│  ├─ channels/             §3.2 对外/对内渠道类型隔离
│  ├─ breaker.py            §5  L6 熔断
│  ├─ audit.py              §5  L5 审计日志（actor 是第一等公民）
│  ├─ orchestrator.py       §3.1 每日扫描主流程 + 48h 缓冲状态机
│  ├─ summarizer.py         唯一的 LLM 调用：告警里的沟通历史摘要（失败即回退）
│  ├─ report.py             §6  周报与成功指标
│  ├─ verify.py             风险边界自检
│  └─ demo.py               演示剧本
├─ dashboard/index.html     可视化面板
├─ data/                    CSV「财务系统」+ outbox + 审计日志
└─ tests/                   58 项单元测试
```

### 数据文件

| 文件 | 说明 |
|---|---|
| `data/receivables.csv` | 应收账款主表。前 7 列是业务数据（Agent **只读**），后 7 列是 Agent 状态字段（Agent 可写） |
| `data/customers.csv` | 客户主数据：VIP 标签、历史逾期次数、对应客户经理 |
| `data/inbox.csv` | 客户回复，触发关键词强制升级 |
| `data/action_log.csv` | 审计日志，每条动作的 actor / 依据 / 命中策略 |
| `data/outbox/*.json` | dry-run 模式下"发出"的消息全文 |

这些都能直接用 Excel 打开 —— 演示时给对方看原始数据比看界面更有说服力。

---

## 设计要点

> 这一节是面试时真正会被追问的部分。

### 1. 判定权在规则层，解释权才交给模型

整条催收流程的**决策部分没有一次 LLM 调用**。分级靠阈值，强制升级靠关键词匹配，
动作选择靠状态机 —— 全是确定性代码。

LLM 在这套系统里只有一个位置：`orchestrator.summarize_history()`，
把历史动作和客户原话压成一段人话摘要，附在高风险告警里给财务看。
它出错的代价只是摘要不好读，不影响任何分级或发送决策。

这个分工是刻意的：**把"不能出错的部分"和"出错也只是体验变差的部分"分开。**

### 2. L4 策略层跑在 LLM 之外，且只能收紧不能放宽

system prompt 里写"任何还款条件变更必须转人工"是**软约束**——
可能被忽略、被 prompt injection 绕过、在长上下文里遗忘。

`policy.py` 是**硬约束**：它是 Python 代码，不读 prompt、不调模型，只看事实与阈值。
Agent 没有任何办法说服它。

每条规则只能让结果**更严格**（升级、禁止对外发送），不能放宽。
这个单调性保证新增规则永远不会意外打开一个之前关着的口子。

| 规则 | 内容 |
|---|---|
| P-01 | 还款条件变更诉求（分期/打折/延期）→ 强制高风险 + 禁止对外 |
| P-02 | 账目争议 / 法务信号 → 强制高风险 + 禁止对外 |
| P-03 | 金额超红线 → 无论分级都禁止 Agent 自动处理 |
| P-04 | VIP 客户 → 一律转人工（对应护栏指标"VIP 被自动催收 = 0"） |
| P-05 | 单日发送量超限 → 全局暂停并告警 |
| P-06 | 客户声称已付款 → 暂停催收，待财务核账 |

### 3. 非对称风险控制

高风险分级**只触发内部通知，不触发任何对外动作**。

所以误判的代价是"财务多看一眼"，而不是"客户被误伤"。
宁可误报增加人工审查，不可漏报导致自动化伤害客户关系——
这是分级引擎"由高到低短路判定"的原因：一条账款只可能被判得更严，不会被判得更松。

`verify` 里有一条端到端断言直接验证这一点：跑完整轮扫描后，
outbox 里发给高风险账款客户的消息数必须为 0。

### 4. Agent 能力边界写在接口里，不是写在 prompt 里

模板渲染函数的签名是：

```python
render(template_id: str, variables: dict) -> tuple[str, str]
```

**没有 body / text / content 参数。** Agent 能选模板、能填金额日期单号，
但无法凭空造一句"我们同意贵司分三期支付"。这不是"我们要求它别这么做"，是**它做不到**。

再加一层纵深防御：渲染完的对外内容还要过一遍禁用词扫描
（承诺类：分期/打折/延期/免除；施压类：起诉/黑名单/后果自负/立即）。
假设第一层被绕过——模板被误改、变量注入带进危险措辞——这里还能拦住。

对内模板不做这个校验：财务本来就需要在告警里看到"客户要求分期""建议评估是否起诉"
才能决策。对内对外用不同尺度，是有意为之。

### 5. 对外与对内渠道在类型系统层面分开

```
CustomerChannel  →  WhatsApp / Gmail(对客户)     会打扰到客户
InternalChannel  →  Slack / Gmail(对团队)        只打扰同事
```

做成两个基类而不是一个 `send()` 加个 flag，因为 **flag 会写错，类型不会**。
对内模板从对外渠道发送会被直接拒绝（`mode="blocked"`）。

### 6. 接管是显式命令，不是"在 Slack 回一句我来处理"

Agent **不读** Slack 里同事说了什么。要接管，走 `ar takeover INV-xxxx --by 姓名`。

聊天记录是**上下文**，不是**授权**。把它当授权用，
等于给任何能在那个频道说话的人开了一个操作系统的口子。

### 7. 48 小时缓冲期是产品选择，不是技术限制

中度分级不是"Agent 先发了再说"，而是：先 Slack 通知客户经理，
明确告诉他有 48 小时优先处理权、以及不响应会发生什么，到期未响应 Agent 才补位。

补位发的是"正式但克制"的对账函，不是加压话术；
同时把"客户经理未响应"这个事实记进周报——这本身是个管理信号。

设计的是**人机协作**，不是**机器接管**。

### 8. 护栏指标放进 CFO 每周都看的报告里

周报第三段是反向指标（VIP 被自动催收次数、错误升级率，目标都是 0）。

放在报告里而不是藏在监控后台，是有意的：
如果护栏指标只有做系统的人看得到，它就不会真正约束决策。

而且每个护栏指标都有一条运行时规则托底——指标定成 0，
就不能只靠"希望它不发生"，得有代码保证它不可能发生。

---

## 接真实 API

默认全部 dry-run。要真的发消息，需要**同时**满足两个条件：

1. `config.json` 里对应渠道 `"live": true`
2. 环境变量 `AR_LIVE=1`

双重闸门：即使误改了配置文件，没有显式设 `AR_LIVE` 也不会对客户发出真实消息。

复制 `.env.example` 为 `.env` 并填入凭据，然后：

```bash
$env:AR_LIVE="1"; python ar.py scan
```

| 渠道 | 需要的凭据 | 实现位置 |
|---|---|---|
| WhatsApp | `WHATSAPP_PHONE_NUMBER_ID` / `WHATSAPP_TOKEN`（Meta Cloud API） | `channels/whatsapp.py` |
| Gmail | `GMAIL_USER` / `GMAIL_APP_PASSWORD`（SMTP + 应用专用密码） | `channels/gmail.py` |
| Slack | `SLACK_WEBHOOK_URL` 或 `SLACK_BOT_TOKEN` | `channels/slack.py` |
| Google Sheets | `GOOGLE_SERVICE_ACCOUNT_JSON` / `GOOGLE_SHEET_ID` | `store/gsheets_store.py` |

**建议顺序**：先只开 Slack（对内渠道，误发的代价是同事多看一条通知），
确认一切正常再开 Gmail，最后才开 WhatsApp。

> WhatsApp 生产注意：Meta 规定商家只能在客户 24 小时会话窗口内自由发文本，
> 窗口外必须用预审批的 Message Template。这跟我们"只发预设模板"的 L2 约束刚好同向 ——
> 平台合规要求和风险设计是一致的，不是额外负担。

### 换掉 CSV 存储

`store/gsheets_store.py` 已实现 Google Sheets 适配器（需 `pip install google-api-python-client`）。
上层分级引擎与策略层不需要改一行——它们只依赖 `ReceivablesStore` 抽象接口。

真上生产建议换成数据库并用**列级权限的视图**，把 L3 的字段白名单从代码层下沉到数据库层。
目前 Sheets API 没有列级权限，所以白名单由 `store/base.py` 在代码层强制。

### LLM 接入点

整个 Agent 只有一个地方调用 LLM：`summarizer.py`，给**对内**高风险告警生成沟通历史摘要。
它不参与任何决策，出错只影响可读性，所以按"失败即回退"设计：

- `config.json` 的 `llm.provider`：`none`（默认）| `ollama`（本地免费）| `anthropic`；环境变量 `AR_LLM_PROVIDER` / `AR_LLM_MODEL` 可覆盖。
- 摘要里出现的每个数字都必须在原始记录里出现过，否则丢弃摘要、回退为原始清单 —— 模型不能编出金额或日期。
- 客户原话放在 `<facts>` 里作为数据传入，提示词明确它不是指令。
- 超时、报错、空输出、超长 —— 一律回退，扫描照常完成（有单元测试覆盖）。
- AI 摘要下方始终附原始记录，财务可以直接核对。

```bash
$env:AR_LLM_PROVIDER="ollama"; python ar.py demo
```

**不要**把分级判断、关键词识别或发送决策交给模型：那样会丢掉可审计性、
确定性和抗注入能力，而这三点正是这个方案区别于"纯 prompt 驱动 Agent"的地方。

---

## 已知边界

诚实标注，免得演示时被问住：

- **DSO 是代理指标**。严格 DSO 需要期间赊销收入，那个数字在 ERP 里、不在这张应收表上。
  这里用"金额加权平均在外天数"代替，周报里明确标注了口径。宁可标注清楚，不给一个看着精确的错数。
- **CSV 存储每次写回都重写整表**。30-200 笔的演示规模完全无感；
  真实量级要换 `store/` 里的数据库适配器。
- **关键词表是中文 + 少量英文**。PM 文档 §一 Non-Goals 明确第一期不做多语言。
- **没有真正的定时调度**。生产环境用 Windows 任务计划 / cron 每天调一次 `python ar.py scan` 即可。
- **`ar.py paid` 属于人工动作**，它写的是 `status` 这类业务字段——
  Agent 的 Service Account 对这些字段没有写权限，CLI 里也按人工身份记录审计日志。

---

## 环境

Windows 11 · Python 3.14 · 零第三方依赖（仅接真实 Google Sheets 时才需要装包）。

终端里中文或 emoji 显示成乱码，说明控制台编码不是 UTF-8。
CLI 入口已自动处理（`sys.stdout.reconfigure`），如仍有问题：

```bash
chcp 65001
```
