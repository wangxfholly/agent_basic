# mega_agent

> 单文件、可教学、可运行的 Claude Code 风格 Agent 内核 —— 把 s0–s19 教程系列的所有能力(权限闸 / 钩子 / 重试预算 / 任务图 / Worktree 隔离 / 后台任务 + Cron / 多 Agent 团队 / MCP / LLM Gateway / 加密 Profile / 路由)整合到一个 ~1700 行的 Python 文件里。

[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## 目录

- [它是什么](#它是什么)
- [架构总览(L0–L7)](#架构总览-l0l7)
- [安装](#安装)
- [5 分钟跑起来](#5-分钟跑起来)
- [配置:Profiles & Routing](#配置profiles--routing)
- [REPL 命令](#repl-命令)
- [环境变量](#环境变量)
- [运行时目录](#运行时目录)
- [常见用法示例](#常见用法示例)
- [安全:Profile 加密](#安全profile-加密)
- [扩展开发](#扩展开发)
- [故障排查](#故障排查)
- [License](#license)

---

## 它是什么

`mega_agent.py` 是一个**用于教学和实验**的 Agent 内核,把"Claude Code"风格的能力做成了**单文件可读**、**逐层叠加**的最小实现:

- 内置 **29 个原生工具**(read/write/edit/grep/glob/bash/...)
- 一套 **能力权限闸**(read/write/exec 三类粒度,auto/strict 两种模式)
- 可插拔的 **PreToolUse / PostToolUse 钩子**
- **重试预算** + **审计日志**
- **任务图**(DAG)+ **Git Worktree 隔离**(并行子任务互不串改)
- **后台任务 + Cron** 调度
- **多 Agent 团队**(协作 / 评审)
- **MCP** 工具桥接
- **LLM Gateway 适配层**:同一套代码可对接 Anthropic / OpenAI / 任意兼容网关 / Mock
- **加密 Profile + 路由表**:多模型多 API Key 配置化管理,支持 `@code 写一段 Python` 这种按路由派发

> 它**不是**生产级 Agent 框架。它的目标是:把 Agent 系统每一层的本质用最少的代码讲清楚,便于二次开发或学习。

---

## 架构总览 (L0–L7)

| 层 | 名称 | 关键能力 | 对应教程 |
|---|---|---|---|
| L0 | **Kernel** | 工具注册表、ToolUse 解析、推理主循环 | s0–s5 |
| L1 | **Permissions** | 能力分类 + auto/strict 模式 + 路径白名单 | s6 |
| L2 | **Hooks** | PreToolUse / PostToolUse,可改写参数 / 拦截 | s7 |
| L3 | **Retry & Audit** | 重试预算、JSONL 审计日志、事件总线 | s8 |
| L4 | **Task Graph + Worktree** | DAG 调度 + 每节点独立 git worktree | s12, s18 |
| L5 | **Background + Cron** | 异步任务 + 简版 cron 表达式 | s14 |
| L6 | **Teams** | 多 Agent 协作、评审者模式 | s16 |
| L7 | **MCP + Gateway + Profiles** | MCP 桥接 + LLM 多后端 + 加密 Profile/Routing | s17, s19 |

---

## 安装

### 1. 克隆 & 装依赖

```bash
git clone https://github.com/wangxfholly/agent_basic.git
cd agent_basic
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

依赖说明:

| 包 | 何时需要 |
|---|---|
| `anthropic` | 直连 Anthropic API(`protocol=anthropic`) |
| `openai` | 走 OpenAI 或任何 OpenAI 兼容网关(`protocol=openai`) |
| `cryptography` | 启用 Profile 加密(设置了 `AGENT_MASTER_PASSWORD`) |

如果只用其中一个协议,可以只装对应的 SDK。

### 2. 系统要求

- Python ≥ **3.10**
- `git` 命令行(任务图 + worktree 需要)
- 一个有效的 LLM API Key

---

## 5 分钟跑起来

最小配置:用环境变量直接接 Anthropic 官方 API,无需 Profile。

```bash
export ANTHROPIC_API_KEY=sk-ant-xxxxx
export AGENT_LLM_BACKEND=anthropic        # 默认就是这个
export AGENT_MODEL=claude-sonnet-4-20250514

python3 mega_agent.py
```

启动后看到:

```
== mega_agent ==
WORKDIR   = /your/path
REPO_ROOT = /your/path
tools     = 29 native + 0 mcp
secrets   = DISABLED (plaintext)
profile   = (none — use /add-profile or env)
models    = /your/path/models.json
commands  : /profiles  /use <name>  /add-profile  /rm-profile <name>
            /routes  /route <name> <profile>  /rm-route <name>
            /perm <auto|strict>  /quit
syntax    : '@<route> <prompt>'  → run with routed model
you>
```

试一下:

```text
you> 列出当前目录的文件,然后把 README.md 的标题打印出来
```

---

## 配置:Profiles & Routing

如果你要管理 **多个 API Key + 多个模型 + 多个网关**,推荐用内置的 Profile 系统替代环境变量。

### 概念

- **Profile** = `{name, protocol, model, base_url, api_key}`,即"一组完整的连接配置"
- **Routing** = `{语义名 → profile 名}`,例如 `code → claude-sonnet`、`write → gpt-4o`
- **Active Profile** = 默认使用的 profile(`/use <name>` 切换)

### 添加第一个 Profile

```text
you> /add-profile
  name      : claude
  protocol  [anthropic|openai|mock]: anthropic
  model     : claude-sonnet-4-20250514
  base_url  (empty=default):
  api_key   : sk-ant-xxxxxxx
saved → /your/path/models.json

you> /use claude
active profile = claude (anthropic / claude-sonnet-4-20250514)
```

### 加一个走 OpenAI 兼容网关的 Profile

```text
you> /add-profile
  name      : gpt4o
  protocol  [anthropic|openai|mock]: openai
  model     : gpt-4o
  base_url  : https://your-gateway.example.com/v1
  api_key   : sk-xxxxxxxx
```

> `protocol=openai` 时,任何**实现了 OpenAI Chat Completions 协议**的网关都能用(LiteLLM、One API、Together、Groq、自建 vLLM 等)。

### 配置路由表

让"写代码"和"写文章"用不同模型:

```text
you> /route code   claude
you> /route write  gpt4o
you> /route default claude
you> /routes
  code            → claude
  write           → gpt4o
  default         → claude
```

### 按路由调用

```text
you> @code 帮我写个 Python 装饰器,统计函数耗时
you> @write 帮我润色一下 README 的引言
you> 普通问题不带 @ 前缀,会用 active profile
```

---

## REPL 命令

| 命令 | 作用 |
|---|---|
| `/profiles` | 列出所有 profile,`*` 标记 active |
| `/use <name>` | 切换 active profile |
| `/add-profile` | 交互式添加 profile(name / protocol / model / base_url / api_key) |
| `/rm-profile <name>` | 删除 profile |
| `/routes` | 列出路由表 |
| `/route <name> <profile>` | 设置路由,如 `/route code claude` |
| `/rm-route <name>` | 删除路由 |
| `/perm auto` / `/perm strict` | 切换权限模式 |
| `/backend anthropic\|openai\|mock` | 临时切换 LLM 后端(覆盖 profile) |
| `@<route> <prompt>` | 按路由派发,如 `@code 写个二分查找` |
| `/quit` / `/exit` | 退出 |

---

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `AGENT_WORKDIR` | `.` | Agent 的工作目录 |
| `AGENT_MODEL` | `claude-sonnet-4-20250514` | 没有 profile 时使用的默认模型 |
| `AGENT_MAX_ITERS` | `50` | Agent loop 最大轮数 |
| `AGENT_PERM_MODE` | `auto` | 权限模式 `auto` / `strict` |
| `AGENT_RETRY_THRESHOLD` | `3` | 同一工具失败几次后熔断 |
| `AGENT_LLM_BACKEND` | `anthropic` | 默认 LLM 后端,可选 `anthropic` / `openai` / `mock` |
| `AGENT_LLM_PROFILE` | _(空)_ | 启动时要激活的 profile 名 |
| `AGENT_GATEWAY_BASE_URL` | _(空)_ | OpenAI 兼容网关地址(也接受 `OPENAI_BASE_URL`) |
| `AGENT_GATEWAY_API_KEY` | _(空)_ | 网关 API Key(也接受 `OPENAI_API_KEY`) |
| `ANTHROPIC_API_KEY` | _(空)_ | Anthropic 官方 Key |
| `AGENT_MASTER_PASSWORD` | _(空)_ | **设置后启用 Profile 加密**(详见下方安全章节) |

> 优先级:`@route` > `/use` 选定的 active profile > `AGENT_LLM_PROFILE` > 环境变量 fallback

---

## 运行时目录

启动后会在工作目录生成以下文件 / 目录(全部已加入 `.gitignore`):

| 路径 | 用途 |
|---|---|
| `models.json` | 明文 Profile 存储(未启用加密时) |
| `models.json.enc` | 加密 Profile 存储(启用加密时) |
| `.master-key.salt` | 加密用的 PBKDF2 salt |
| `.tasks/` | 任务图节点状态 |
| `.worktrees/` | 子任务的 git worktree 副本 |
| `.team/` | 多 Agent 团队协作产物 |
| `.runtime-tasks/` | 后台任务记录 |
| `.cron/` | Cron 触发器状态 |
| `CLAUDE.md` | 用户个性化记忆(可选) |
| `.hooks.json` | 用户自定义钩子配置 |

---

## 常见用法示例

### 1. 让 Agent 改代码 + 跑测试 + 提交

```text
you> 把 src/util.py 里的 add 函数加上类型注解,然后运行 pytest,如果通过就 git commit
```

Agent 会自动:read → edit → bash(pytest) → bash(git commit)。每一步都受权限闸约束。

### 2. 切到严格模式审视危险操作

```text
you> /perm strict
perm mode = strict
you> 删掉 /tmp 下所有文件
[permission] denied: bash 'rm -rf /tmp/*' requires confirmation in strict mode
```

### 3. 跨模型协作

```text
you> /route code claude
you> /route review gpt4o
you> @code 写一个 LRU 缓存的 Python 实现
... (claude 输出)
you> @review 评审上面那段代码,指出潜在问题
... (gpt4o 输出)
```

### 4. 同时管理多个 API Key

```text
you> /add-profile        # 个人账号 OpenAI Key
you> /add-profile        # 公司账号 Azure Key
you> /add-profile        # 自建 vLLM 网关
you> /profiles
   personal-gpt   openai     gpt-4o                key=sk-...
 * company-azure  openai     gpt-4-turbo           key=ak-...
   self-vllm     openai     llama-3.1-70b         key=local
```

---

## 安全:Profile 加密

默认 `models.json` 是明文存的。如果你要把仓库 / 备份 / 同步到不可信环境,**强烈建议启用加密**。

### 启用

```bash
export AGENT_MASTER_PASSWORD='your strong passphrase'
python3 mega_agent.py
```

启动时:

- 旧的 `models.json`(明文)会**自动迁移**为 `models.json.enc`(密文)
- 明文文件被删除
- 后续读写全部走 Fernet 对称加密(PBKDF2-HMAC-SHA256, 200,000 轮, 16 字节 salt)

启动横幅会变成:

```
secrets   = ENABLED (Fernet/PBKDF2)
models    = /your/path/models.json.enc
```

### 注意

- 密码错了会直接拒绝启动,不会破坏密文
- `.master-key.salt` 必须和 `.enc` 文件**保持一对**,丢了等于丢密钥
- 二者**都已经在 `.gitignore` 中**,不会被误提交
- 想换密码?先用旧密码启动 → `/profiles` 看一遍内容 → 退出 → 改 `AGENT_MASTER_PASSWORD` → 删掉 `.enc` 和 `salt` → 重启 → `/add-profile` 重新填一遍

---

## 扩展开发

### 加一个新工具

在 `mega_agent.py` 里搜 `TOOL_HANDLERS = {`,照已有工具的 schema 加一个就行:

```python
@register_tool(
    name="my_tool",
    description="...",
    input_schema={...},
    capabilities=["read"],   # 或 ["write"], ["exec"]
)
def my_tool(input: dict) -> str:
    ...
```

### 加一个新 LLM 后端

继承 `LLMClient` 抽象类,实现 `complete(messages, tools, system) -> LLMResponse`,然后在 `make_llm_client()` 的分支里加一支即可。

### 加一个 PreToolUse 钩子

在 `.hooks.json` 里写:

```json
[
  {"event": "PreToolUse", "match": {"tool": "bash"}, "action": "deny",
   "reason": "bash disabled by org policy"}
]
```

---

## 故障排查

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: anthropic` | 没装 SDK | `pip install anthropic` |
| `ModuleNotFoundError: cryptography` | 启用了加密但没装库 | `pip install cryptography` |
| 启动报 `wrong password / corrupted file` | 密码错了 | 检查 `AGENT_MASTER_PASSWORD` |
| `[llm] mock / mock-model` | 没配 Key,回退到 mock | 设置 API Key 或 `/use <profile>` |
| `permission denied: write requires ...` | strict 模式下需要确认 | `/perm auto` 或在交互中确认 |
| Worktree 创建失败 `Author identity unknown` | git 没设 user.name/email | `git config --global user.name "..."` |
| `@code` 报 `unknown route` | 路由表没配 | `/route code <profile-name>` |

---

## License

[MIT](LICENSE) © 2026 wangxfholly
