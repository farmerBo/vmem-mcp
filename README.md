# vmem — 项目记忆检索层

> **TL;DR** — A zero-dependency, single-file MCP server that turns Markdown project memory into a searchable SQLite FTS5 index and auto-injects the relevant bits at session start. Original `.md` files are never modified or deleted. No embedding model, no vector DB, no API key, no daemon.

**Repo**: https://github.com/farmerBo/vmem-mcp · MIT © 2026 岁月静静好

```bash
git clone https://github.com/farmerBo/vmem-mcp.git
```

长周期项目里，记忆文件会涨到几十万字：全读必然撑爆上下文，按日期猜又猜不中哪天记了什么，反复压缩蒸馏则越压越丢细节。vmem 的做法是 **原文永不删 + 建立可随时重建的索引 + hook 自动注入**。

## 为什么默认不上向量

技术记忆里充满精确标识符：版本号、配置键、错误码、字段名、文件路径。embedding 会把它们糊成「相似」，查 `config_key_a` 可能召回 `config_key_b`。因此精确词法检索是硬性底座，向量最多作为补充。

这不是特立独行：GitHub 上最成熟的向量记忆服务 [mcp-memory-service](https://github.com/doobidoo/mcp-memory-service) 的 README 同样写着 "Combine vector search with BM25"，并在语义结果稀疏时回退到精确 / tag 匹配；面向 WorkBuddy 的 [workbuddy-memory-system](https://github.com/Xavier-06/workbuddy-memory-system) 架构文档也明确「文件记忆是精确查找，向量搜索是语义匹配」「向量索引是 embedding 副本，纯检索层，不存独立内容」。

vmem 把这条共识里**必需的那半边**做成零依赖实现。`search_db()` 保留了 RRF 融合接口，日后要接本地模型只需追加一路候选，不影响现有能力。

## 架构

```
.workbuddy/memory/*.md   ← 唯一真相源（append-only，人类可读可编辑）
        │  index：按 mtime 增量，Markdown 标题切块 + 滑窗
        ▼
vmem.db                  ← SQLite + FTS5 trigram；可随时 rebuild 的索引
        │  search / hook-inject
        ▼
MCP 工具  ·  SessionStart hook-inject(additionalContext)  ·  CLI
```

每条记录都带 `source`（文件:行号）与标题面包屑，回答可回溯原文。

## 安装

三层能力里 **MCP 是通用的**（`vmem.py` 是标准 stdio JSON-RPC），hook 与 skill 则各有各的配置格式，**不能把 WorkBuddy 的配置原样拷给 Codex**。

| 客户端 | MCP server 配置 | SessionStart hook | skill 纪律 |
|---|---|---|---|
| WorkBuddy | `~/.workbuddy/mcp.json`（JSON） | `~/.workbuddy/settings.json` → `hooks.SessionStart` | `~/.workbuddy/skills/vmem/SKILL.md` |
| Claude Code | `~/.claude.json` 或项目 `.mcp.json`（JSON） | `~/.claude/settings.json` → `hooks.SessionStart` | `~/.claude/skills/vmem/SKILL.md` |
| Codex CLI | `~/.codex/config.toml` → `[mcp_servers.vmem]`（TOML） | `~/.codex/hooks.json` | ⚠️ 用 `skill.toml` + `instructions.md`，**SKILL.md 不能直接用** |

**WorkBuddy / Claude Code**（hook 格式相同）：

1. 把 `vmem.py` 放到任意固定目录。
2. 合并 [`examples/mcp.json`](examples/mcp.json) 到对应的 MCP 配置文件，把路径改成你自己的。
3. WorkBuddy 需在连接器管理页右上角「自定义连接器」里对 `vmem` 点一次「信任」（新 MCP 不会自动激活）。
4. 把 [`examples/sessionstart-hook.json`](examples/sessionstart-hook.json) 这条命令**追加**到 `settings.json` 的 `hooks.SessionStart` 数组，不要覆盖已有钩子。
5. 可选：把 [`skill/SKILL.md`](skill/SKILL.md) 复制为用户级 skill，用于约束 AI「何时必查、何时写回、结果必须带出处」。

**Codex CLI**：用 [`examples/codex-config.toml`](examples/codex-config.toml) 和 [`examples/codex-hooks.json`](examples/codex-hooks.json)。三点差异：配置是 TOML、hook 单独放 `hooks.json`、非 managed hook 要在 `/hooks` 里 trust 一次（改过命令会要求重新 trust）。skill 那层可以直接跳过 —— MCP + hook 已经够用，不必强行移植。

★ **hook 输出契约**：`hook-inject` 同时输出 `systemMessage`（一句短提示）和 `hookSpecificOutput.additionalContext`（记忆正文）。Claude Code / Codex 里 `systemMessage` 只是给用户看的 UI 警告，**只有 `additionalContext` 会进模型上下文**；早期版本只输出 `systemMessage`，等于没注入，v0.2.1 已修。

**共同前提**：Python 3.9+ 且 SQLite 带 FTS5 `trigram`；记忆原文放在项目的 `.workbuddy/memory/*.md`（别的目录用 `VMEM_MEMORY_DIR` 覆盖）；**MCP server 的启动 cwd 决定项目归属**，多项目时给 server 配 `cwd` 或在工具调用里显式传 `project`。

首次建索引：

```bash
python vmem.py index --cwd /path/to/project
python vmem.py stats --cwd /path/to/project
python vmem.py search "config_key 超时" -k 5 --cwd /path/to/project
```

之后 **无需手动维护**：`hook-inject` 会在每次会话启动时先静默跑一遍增量索引，直接编辑 `.md` 追加的内容也会进本轮注入。

## CLI

| 命令 | 用途 |
|---|---|
| `mcp` | 启动 MCP server（stdio JSON-RPC，也是无参数时的默认行为） |
| `index [--force] [--cwd P]` | 增量扫描 `.md` 入库；`--force` 全量重建 |
| `search QUERY [-k 6] [--json]` | 检索，默认输出带出处的 top-k |
| `add --text TEXT [--kind K]` | 追加当日 `.md` 并入库，用于口头约定 / 临时拍板 |
| `get ID` | 取某条完整原文 |
| `stats` | 块数 / 文件数 / 类型分布 / db 大小 |
| `hook-inject [--budget 2600]` | 给 `SessionStart` 用，输出 `{"continue","systemMessage","hookSpecificOutput.additionalContext"}` |
| `rebuild` | 等价于 `index --force` |

所有命令都支持 `--cwd` 指定工作区、`--project` 显式指定项目 id。找不到 `.workbuddy` 时会回退到注册表里最近使用的项目。

## MCP 工具

`memory_search` · `memory_add` · `memory_get` · `memory_index` · `memory_stats`

## 检索与排序

- **FTS5 `trigram`**：中文按 3 字切，BM25 打分，标题列权重 2.0。
- **短词走 LIKE**：踩过的坑 —— trigram 最少 3 字符，`"阈值"` 这类 2 字词送进 `MATCH` 恒为 0 命中，会让 `identifier 中文词` 混合查询退化成只按 identifier 排。现在短词改走 LIKE 并参与严格交集。
- **严格路 / 宽松路 RRF 融合**：要求全部词命中的结果权重 1.0，任一命中的兜底路权重 0.4（严格路为空时升为 1.0）。换说法、少一个词时不会零召回。
- **加权**：`importance`、`kind=rule/curated` ×1.25、近 30 天 ×1.1、被 `supersedes` 的旧结论 ×0.4。
- 注入有 2600 字符硬预算，不会把上下文吃满。

## 关键设计取舍

| 决定 | 理由 |
|---|---|
| 原文 `.md` 是唯一真相源 | db 只是索引，损坏就 `rebuild`；杜绝有损蒸馏 |
| 每项目一个 db | 跟项目走，备份 / 迁移 / 删除都干净；跨项目用 `registry.json` |
| hook 先索引再注入 | 不依赖 AI「想起来调用工具」，也不依赖写日志时用了哪个工具 |
| WAL + `synchronous=NORMAL` | Windows 上 fsync 很贵；db 可重建，无需 FULL 级别的耐久性 |
| 不启用向量 | 精确 token 更重要；要语义可后接，且不应取代词法路 |
| 不做自动会话采集 | 记什么由人决定，避免把噪声写进长期记忆 |

## 已知边界（如实说明）

- 纯词法检索：同一意思的完全不同说法可能漏召回。先换关键词多试一次，比装 90MB 模型便宜。
- 需要 SQLite 带 FTS5 + trigram（SQLite ≥ 3.34，Python 3.9+ 自带版本通常够）。可自检：
  `python -c "import sqlite3;sqlite3.connect(':memory:').execute(\"create virtual table t using fts5(x,tokenize='trigram')\")"`
- 只挂 `SessionStart`，没做 `UserPromptSubmit`（避免每条消息都检索）和 `Stop` 自动写回。
- `hook-inject` 会写库，多会话并发时靠 `busy_timeout=3000` 缓解；异常一律吞掉，stdout 保证只有一行 JSON。
- 不删原文、不去重改写、不自动判定事实真伪；`supersedes` 字段预留，旧结论保留在库里降权展示。

## 实测

在一个 41 个 `.md`、约 60 万字的项目上：导入 1909 块 / 9.5 MB，单次注入 1515 字符。
「英文标识符 + 2 字中文词」的多词查询，修复前 top1 是三条泛泛的关键词日志，
修复后 top1 为对应铁律条目、top2/3 为原始推导日志；单一稀有 token 查询不受影响。

## 与同类项目的分工

- **memory-vault**（定期快照）解决「30 天蒸馏把原始日志删了」；vmem 解决「AI 不会主动去翻日志」。两者可并存。
- **mcp-memory-service** 功能更全（知识图谱、自动捕获、多后端），代价是 ONNX 模型 + 常驻 HTTP 服务 + 数十个工具。vmem 只做它的「精确检索 + 注入」子集，零依赖、单文件、跟项目走。

## 开源

建议仓库名 `vmem-mcp` —— `vmem` 已被 ICCV 2025 的 VMem、`pmem/vmem` 等占用。本目录即为仓库根目录，`registry.json`、`*.db`、`__pycache__` 已在 `.gitignore` 中排除，不含任何本机路径或项目专有数据。

## License

MIT，见 [LICENSE](LICENSE)。
