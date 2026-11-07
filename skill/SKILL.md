---
name: vmem
description: |
  通用项目记忆检索层（MCP + hooks）。This skill should be used when 开工需要回忆项目历史决策、
  技术真值、已踩过的坑或「之前是怎么定的」；在写码或下结论前需要核对旧口径；
  收工要把本轮结论写回记忆；或用户提到「记忆库 / 向量记忆 / 搜一下之前记过的 /
  别再压缩蒸馏 / vmem / 记忆太多读不过来」。
  暴露 memory_search / memory_add / memory_get / memory_index / memory_stats 五个 MCP 工具，
  并由 SessionStart hook 自动注入核心记忆。
  DO NOT TRIGGER when 纯编码任务且不需要任何项目历史上下文。
---

# vmem —— 通用项目记忆检索层

## 定位

项目原文（`.workbuddy/memory/*.md`）是**唯一真相源**；`vmem.db` 只是可随时重建的索引，
永不删原文、不做有损压缩蒸馏。每个项目一个 SQLite 文件，互不干扰。

若同时装了 `memory-vault` 一类快照工具，不要混用：它解决「定期蒸馏把原始日志删了」，
vmem 解决「AI 不会主动去翻日志」。

## 铁律

1. **不整份读取**记忆目录下的 `.md`（大项目几十万字，全读必然撑爆上下文）。
   一律用 `memory_search` 或 CLI 检索，只消费返回的 top-k 片段。
2. **写码、改数值、下结论前先检索**该主题的历史口径。引用结论必须带上结果里的
   `source`（文件:行号）与 `heading`，便于回溯原文；查不到就明说「索引里没有」，不要凭印象编。
3. **收工写回**：把本轮有跨会话价值的结论追加到当日 `.md`。用 `memory_add` 可同时写
   原文与索引；直接编辑 `.md` 也可以 —— `SessionStart` 的 `hook-inject` 会先按 mtime
   增量索引再注入，不会漏。
4. **不删原文、不覆盖旧结论**。修正过的旧口径保留在库里（`supersedes` 预留），
   新结论另起条目；索引损坏时 `rebuild`，绝不拿摘要替代原文。
5. **精确 token 用原词查**：版本号、配置键、错误码、函数名靠词法精确匹配最准，
   中文短语可整句输入，混合「标识符 + 中文短词」也可。默认未启用向量，
   不要向用户宣称「语义检索」；同一意思的完全不同说法可能漏召回，换关键词再试一次即可。

## 工具

MCP（推荐，任何会话可调）：

| 工具 | 用途 |
|---|---|
| `memory_search(query, top_k=6)` | 查历史；返回带 source / heading / kind / score 的片段 |
| `memory_add(text, kind, tags)` | 记口头约定、临时拍板；同时追加当日 .md 并入索引 |
| `memory_get(id)` | 取某条完整原文 |
| `memory_index(force)` | 增量扫描 .md；`force=true` 全量重建 |
| `memory_stats()` | 块数 / 文件数 / 类型分布 / db 大小 / 向量是否启用 |

CLI（不开客户端也能查，把路径换成你自己的）：

```bash
python /path/to/vmem.py index   --cwd /path/to/project
python /path/to/vmem.py search "配置键 超时" -k 5 --cwd /path/to/project
python /path/to/vmem.py rebuild --cwd /path/to/project
python /path/to/vmem.py stats   --cwd /path/to/project
```

## 自动注入

`SessionStart` hook 挂 `vmem.py hook-inject`，输出 `{"continue":true,"systemMessage":...}`，
把「核心铁律 + 最近条目」按 ≤2600 字符预算注入会话上下文。这一步不依赖 AI 自觉，
且会先做一次静默增量索引。关闭方式：从 `settings.json` 的 `SessionStart` 数组移除该条。

## 排障

- 报「索引不存在」→ 先 `memory_index`（或任意一次会话启动会自动补）。
- 换了项目目录 → 工具按 cwd 向上找 `.workbuddy`，也可用 `project` 参数指定；
  注册表在 `~/.workbuddy/vmem/registry.json`（可用 `VMEM_HOME` 覆盖）。
- 记忆目录不是默认路径 → 设 `VMEM_MEMORY_DIR` 环境变量。
- MCP 未生效 → 检查 `mcp.json`；新 server 需在连接器管理页手动「信任」，不会自动激活。
- 中文 2 字词查不到 → v0.2 起短词已改走 LIKE；若仍异常，先 `rebuild --force`。
- 想启用向量 → `search_db()` 预留了 RRF 融合接口，可追加本地 embedding 候选路；
  但不建议取代词法路，装模型前先确认确实存在「换说法查不到」的实际用例。
