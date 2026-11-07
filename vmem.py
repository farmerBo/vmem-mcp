# -*- coding: utf-8 -*-
"""vmem —— 通用项目记忆库（MCP + CLI + hooks），单文件、零依赖、跨项目。

设计要点：
  · 原文 .md 是唯一真相源，本库只是**可随时 rebuild 的索引**，永不删原文、不做有损蒸馏
  · 每个项目一个 SQLite 文件：{workspace}/.workbuddy/memory/vmem.db
  · 检索 = FTS5 trigram BM25（AND 优先 / OR 兜底）+ 短词 LIKE 兜底 + 加权
      ⇒ 技术记忆里全是精确 token（version tag / config_key / error_code），
        纯向量会把它们糊掉，所以精确检索是硬性底座；RRF 接口预留但默认不启用向量
  · 结果必须带 source(文件:行号) + 标题面包屑，能回溯原文
  · 不靠模型自觉：SessionStart hook 先增量索引，再用 hookSpecificOutput.additionalContext 注入
  · 零第三方依赖，仅 Python 标准库（需 SQLite 带 FTS5 + trigram，Python 3.9+ 一般都有）

v0.2 (2026-11-07)：hook 增量索引闭环、AND 优先排序、heading 加权、
                    synchronous=NORMAL / busy_timeout、VMEM_HOME 可覆盖。
v0.2.1 (2026-11-07)：hook 注入改走 hookSpecificOutput.additionalContext
                    （systemMessage 只给用户看，不进模型上下文）；补 Codex 配置示例。

子命令：
  mcp                         启动 MCP server（stdio JSON-RPC）
  index [--project P] [--force] [--all]
  search QUERY [-k 6] [--project P] [--all] [--json]
  add --text TEXT [--kind K] [--tags a,b] [--project P]
  get ID [--project P]
  stats [--project P] [--all] [--json]
  hook-inject [--budget 2600]     给 SessionStart hook 用，输出 {"continue","systemMessage",
                                  "hookSpecificOutput":{"hookEventName","additionalContext"}}
  rebuild [--project P]           等价于 index --force
"""
import os
import re
import sys
import json
import time
import math
import sqlite3
import hashlib
import argparse
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stdin.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

HOME = os.path.expanduser('~')
VMEM_HOME = os.environ.get('VMEM_HOME') or os.path.join(HOME, '.workbuddy', 'vmem')
MEMORY_REL = (os.environ.get('VMEM_MEMORY_DIR') or '.workbuddy/memory').strip('/\\')
REGISTRY = os.path.join(VMEM_HOME, 'registry.json')
SCHEMA_VER = '1'
MCP_PROTO = '2024-11-05'
DEFAULT_BUDGET = 2600

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  hash TEXT UNIQUE,
  project TEXT,
  file TEXT,
  kind TEXT,
  source TEXT,
  heading TEXT,
  text TEXT,
  tags TEXT,
  importance INTEGER DEFAULT 1,
  created_at TEXT,
  superseded_by INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file);
CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(text, heading, tokenize='trigram');
CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, mtime REAL, size INTEGER, n_chunks INTEGER, indexed_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


# ------------------------------------------------------------------ 项目定位

def find_workspace(start=None):
    p = os.path.abspath(start or os.getcwd())
    origin = p
    while True:
        if os.path.isdir(os.path.join(p, '.workbuddy')) or os.path.isdir(os.path.join(p, '.git')):
            return p
        q = os.path.dirname(p)
        if q == p:
            return origin
        p = q


def project_id(ws, explicit=None):
    return explicit or os.path.basename(ws.rstrip('\\/')) or 'workspace'


def memory_dir(ws):
    return os.path.join(ws, *MEMORY_REL.split('/'))


def db_path(ws):
    return os.path.join(memory_dir(ws), 'vmem.db')


def load_registry():
    try:
        return json.load(open(REGISTRY, encoding='utf-8'))
    except Exception:
        return {}


def save_registry(reg):
    os.makedirs(VMEM_HOME, exist_ok=True)
    json.dump(reg, open(REGISTRY, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)


def register(ws, project):
    reg = load_registry()
    reg[project] = {'workspace': ws, 'db': db_path(ws), 'updated': datetime.now().isoformat(timespec='seconds')}
    reg['_last'] = project
    save_registry(reg)


def resolve(explicit=None, cwd=None):
    """返回 (workspace, project)。找不到 .workbuddy 时回退 registry 最近项目。"""
    ws = find_workspace(cwd)
    project = project_id(ws, explicit)
    if not os.path.isdir(memory_dir(ws)):
        reg = load_registry()
        last = explicit or reg.get('_last')
        if last and last in reg and os.path.isdir(memory_dir(reg[last]['workspace'])):
            ws = reg[last]['workspace']
            project = last
    return ws, project


def connect(ws, create=True):
    p = db_path(ws)
    if not create and not os.path.isfile(p):
        return None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    # Windows 上 fsync 很贵；WAL + NORMAL 已足够安全（原文 .md 才是真相源，db 可重建）
    c.execute('PRAGMA synchronous=NORMAL')
    c.execute('PRAGMA busy_timeout=3000')
    c.executescript(SCHEMA)
    c.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema',?)", (SCHEMA_VER,))
    return c


# ------------------------------------------------------------------ 切块

def slide(text, size=700, overlap=80):
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        j = min(len(text), i + size)
        if j < len(text):
            cut = text.rfind('\n', i + size // 2, j)
            if cut > i:
                j = cut
        out.append(text[i:j].strip())
        if j >= len(text):
            break
        i = max(i + 1, j - overlap)
    return [x for x in out if x]


def guess_kind(rel, heading, text):
    low = (rel + ' ' + heading).lower()
    if rel.lower().startswith('memory.md'):
        return 'curated'
    if '铁律' in heading or '★★★' in text[:120]:
        return 'rule'
    if re.match(r'^\d{4}-\d{2}-\d{2}', rel):
        return 'daily'
    if 'detail' in low:
        return 'detail'
    return 'note'


def guess_tags(heading, kind):
    tags = {kind}
    for t in re.findall(r'[A-Za-z0-9_.\-]{3,}|[\u4e00-\u9fff]{2,6}', heading):
        tags.add(t)
    return sorted(tags)[:10]


def split_md(path, rel):
    try:
        raw = open(path, 'r', encoding='utf-8', errors='replace').read()
    except Exception:
        return []
    lines = raw.splitlines()
    heads, buf, start, cur_head, out = [], [], 1, '', []
    mdate = re.search(r'(\d{4}-\d{2}-\d{2})', rel)
    date = mdate.group(1) if mdate else datetime.fromtimestamp(os.path.getmtime(path)).strftime('%Y-%m-%d')

    def flush():
        body = '\n'.join(buf).strip()
        if not body:
            return
        kind = guess_kind(rel, cur_head, body)
        imp = 3 if '★★★' in body[:120] else (2 if ('★★' in body[:120] or rel.lower().startswith('memory.md')) else 1)
        for piece in slide(body):
            out.append({
                'source': '%s:%d' % (rel, start),
                'heading': cur_head, 'text': piece, 'kind': kind,
                'importance': imp, 'tags': guess_tags(cur_head, kind), 'date': date,
            })

    for i, line in enumerate(lines, 1):
        m = re.match(r'^(#{1,6})\s+(.*)$', line)
        if m:
            flush()
            buf, start = [], i
            lvl, title = len(m.group(1)), m.group(2).strip()
            heads = [(l, t) for l, t in heads if l < lvl]
            heads.append((lvl, title))
            cur_head = ' > '.join(t for _, t in heads)
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return out


def index_file(c, ws, path, project, force=False):
    rel = os.path.relpath(path, memory_dir(ws)).replace('\\', '/')
    try:
        st = os.stat(path)
    except OSError:
        return 0
    row = c.execute('SELECT mtime,size FROM files WHERE path=?', (rel,)).fetchone()
    if not force and row and abs((row['mtime'] or 0) - st.st_mtime) < 1e-6 and row['size'] == st.st_size:
        return 0
    ids = [r[0] for r in c.execute('SELECT id FROM chunks WHERE file=?', (rel,))]
    for i in ids:
        c.execute('DELETE FROM fts WHERE rowid=?', (i,))
    c.execute('DELETE FROM chunks WHERE file=?', (rel,))
    n = 0
    for ch in split_md(path, rel):
        h = hashlib.sha1((rel + '\n' + ch['text']).encode('utf-8')).hexdigest()
        if c.execute('SELECT 1 FROM chunks WHERE hash=?', (h,)).fetchone():
            continue
        cur = c.execute(
            'INSERT INTO chunks(hash,project,file,kind,source,heading,text,tags,importance,created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?)',
            (h, project, rel, ch['kind'], ch['source'], ch['heading'], ch['text'],
             json.dumps(ch['tags'], ensure_ascii=False), ch['importance'], ch['date']))
        rid = cur.lastrowid
        c.execute('INSERT INTO fts(rowid,text,heading) VALUES(?,?,?)', (rid, ch['text'], ch['heading']))
        n += 1
    c.execute('INSERT OR REPLACE INTO files(path,mtime,size,n_chunks,indexed_at) VALUES(?,?,?,?,?)',
              (rel, st.st_mtime, st.st_size, n, datetime.now().isoformat(timespec='seconds')))
    return n


def index_workspace(ws, project, force=False):
    """按 mtime 增量重扫 .md；单次提交，避免 Windows 逐文件 fsync。"""
    c = connect(ws)
    md = memory_dir(ws)
    targets = []
    for dp, dn, fn in os.walk(md):
        for f in fn:
            if f.lower().endswith('.md'):
                targets.append(os.path.join(dp, f))
    total = 0
    for p in sorted(targets):
        total += index_file(c, ws, p, project, force=force)
    c.commit()
    n = c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    c.close()
    return {'project': project, 'workspace': ws, 'files': len(targets),
            'new_chunks': total, 'total_chunks': n, 'db': db_path(ws)}


def cmd_index(args):
    ws, project = resolve(args.project, args.cwd)
    register(ws, project)
    return index_workspace(ws, project, force=args.force)


# ------------------------------------------------------------------ 检索

def query_terms(q):
    terms = []
    for t in re.findall(r'[A-Za-z0-9_.\-]+|[\u4e00-\u9fff]+', q):
        if re.match(r'^[\u4e00-\u9fff]+$', t) and len(t) > 4:
            terms.extend(t[i:i + 3] for i in range(0, min(len(t) - 2, 9)))
        terms.append(t)
    seen, out = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out


def rrf_fuse(rank_lists, k=60):
    out = {}
    for lst, weight in rank_lists:
        for rank, rid in enumerate(lst):
            out[rid] = out.get(rid, 0.0) + weight / (k + rank + 1)
    return out


def search_db(c, q, top_k=6):
    """BM25(trigram) AND 优先 / OR 兜底 + 短词 LIKE 兜底 + 加权；RRF 接口留给可选向量路。"""
    terms = query_terms(q)
    # FTS5 trigram 最少 3 字符：2 字中文词（如「门位」）送进 MATCH 恒 0 命中，
    # 必须改走 LIKE，否则「walkBand 门位」会退化成只按 walkBand 排序。
    fts_terms = [t for t in terms if len(t) >= 3][:14]
    short_terms = [t for t in terms if len(t) < 3][:8]
    quoted = ['"%s"' % t.replace('"', '""') for t in fts_terms]
    fts_and, fts_or = [], []
    if quoted:
        plans = []
        if len(quoted) > 1:
            plans.append((' AND '.join(quoted), fts_and))
        plans.append((' OR '.join(quoted), fts_or))
        for expr, dst in plans:
            try:
                dst.extend(r[0] for r in c.execute(
                    'SELECT rowid FROM fts WHERE fts MATCH ? '
                    'ORDER BY bm25(fts, 1.0, 2.0) LIMIT 200', (expr,)))
            except sqlite3.OperationalError:
                pass

    like_ids, short_sets = [], []
    for t in short_terms:
        try:
            ids = [r[0] for r in c.execute(
                'SELECT id FROM chunks WHERE text LIKE ? LIMIT 400', ('%' + t + '%',))]
        except sqlite3.OperationalError:
            ids = []
        like_ids += ids
        short_sets.append(set(ids))
    if len(q) >= 2:
        try:
            like_ids += [r[0] for r in c.execute(
                'SELECT id FROM chunks WHERE text LIKE ? LIMIT 50', ('%' + q + '%',))]
        except sqlite3.OperationalError:
            pass

    # 严格路 = 所有词都命中（FTS AND ∩ 各短词 LIKE）；只有一个 FTS 词时它自成一部分。
    parts = []
    if fts_and:
        parts.append(set(fts_and))
    elif len(quoted) == 1 and fts_or:
        parts.append(set(fts_or))
    parts += short_sets
    strict = set.intersection(*parts) if parts else set()
    order = {rid: i for i, rid in enumerate(fts_or)}
    strict_list = sorted(strict, key=lambda r: order.get(r, 9999))

    # 宽松路 = 任一词命中，保证换说法 / 少一个词时不会零召回。
    loose = list(dict.fromkeys(fts_or + like_ids))
    # 向量路：日后接入时在此追加 (vector_ids, 0.9)，接口不变，默认不启用
    scores = rrf_fuse([(strict_list, 1.0),
                       (loose, 0.4 if strict_list else 1.0)])
    if not scores:
        return []
    # 按融合分截断：dict 的插入顺序不是排名，直接切片会砍掉加权后本该靠前的条目
    ids = sorted(scores, key=lambda k: -scores[k])[:120]
    marks = ','.join('?' * len(ids))
    rows = c.execute('SELECT * FROM chunks WHERE id IN (%s)' % marks, ids).fetchall()
    cutoff = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    out = []
    for r in rows:
        s = scores[r['id']]
        s *= 1 + 0.3 * ((r['importance'] or 1) - 1)
        if r['kind'] in ('rule', 'curated', 'truth'):
            s *= 1.25
        if (r['created_at'] or '') >= cutoff:
            s *= 1.1
        if r['superseded_by']:
            s *= 0.4
        out.append((s, r))
    out.sort(key=lambda x: -x[0])
    return [{'id': r['id'], 'score': round(s, 6), 'source': r['source'],
             'heading': r['heading'], 'kind': r['kind'], 'date': r['created_at'],
             'text': r['text']} for s, r in out[:top_k]]


def cmd_search(args):
    ws, project = resolve(args.project, args.cwd)
    c = connect(ws, create=False)
    if c is None:
        return {'project': project, 'error': '索引不存在，先运行 index', 'results': []}
    res = search_db(c, args.query, args.k)
    c.close()
    return {'query': args.query, 'project': project, 'count': len(res), 'results': res}


def fmt_results(d, budget=None):
    lines = ['项目 %s · 查询「%s」· %d 条' % (d.get('project', '?'), d.get('query', ''), d.get('count', 0))]
    used = 0
    for i, r in enumerate(d.get('results', []), 1):
        txt = r['text'].replace('\n', ' ').strip()
        if budget:
            room = max(180, budget - used)
            if used >= budget:
                break
            txt = txt[:room]
        used += len(txt)
        lines.append('%d. [%s] %s' % (i, r['source'], txt))
        lines.append('   └ %s · %s · score=%s' % (r['kind'], r['heading'][:60], r['score']))
    return '\n'.join(lines)


# ------------------------------------------------------------------ 写入

def cmd_add(args):
    ws, project = resolve(args.project, args.cwd)
    md = memory_dir(ws)
    os.makedirs(md, exist_ok=True)
    today = datetime.now().strftime('%Y-%m-%d')
    path = os.path.join(md, today + '.md')
    stamp = datetime.now().strftime('%H:%M:%S')
    with open(path, 'a', encoding='utf-8') as f:
        f.write('\n## [vmem %s] %s\n%s\n' % (stamp, args.kind, args.text.strip()))
    c = connect(ws)
    index_file(c, ws, path, project, force=True)
    c.commit()
    n = c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    c.close()
    return {'project': project, 'appended_to': path, 'total_chunks': n}


def cmd_get(args):
    ws, project = resolve(args.project, args.cwd)
    c = connect(ws, create=False)
    if c is None:
        return {'error': '索引不存在'}
    r = c.execute('SELECT * FROM chunks WHERE id=?', (args.id,)).fetchone()
    c.close()
    return dict(r) if r else {'error': '未找到 id=%s' % args.id}


def cmd_stats(args):
    ws, project = resolve(args.project, args.cwd)
    c = connect(ws, create=False)
    if c is None:
        return {'project': project, 'error': '索引不存在'}
    n = c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0]
    nf = c.execute('SELECT COUNT(*) FROM files').fetchone()[0]
    by = {r[0]: r[1] for r in c.execute('SELECT kind,COUNT(*) FROM chunks GROUP BY kind')}
    c.close()
    return {'project': project, 'workspace': ws, 'db': db_path(ws),
            'db_size_kb': round(os.path.getsize(db_path(ws)) / 1024, 1),
            'chunks': n, 'files': nf, 'by_kind': by,
            'vector_enabled': False, 'schema': SCHEMA_VER}


# ------------------------------------------------------------------ hook 注入

def inject_text(ws, budget):
    c = connect(ws, create=False)
    if c is None:
        return ''
    rules = c.execute("SELECT * FROM chunks WHERE kind IN ('rule','curated') "
                      "ORDER BY importance DESC, id DESC LIMIT 3").fetchall()
    recent = c.execute('SELECT * FROM chunks ORDER BY id DESC LIMIT 3').fetchall()
    c.close()
    seen, items = set(), []
    for r in list(rules) + list(recent):
        if r['id'] in seen:
            continue
        seen.add(r['id'])
        items.append(r)
    if not items:
        return ''
    out = ['【项目记忆 · 自动注入】原文 .md 未删改；需要细节用 memory_search 检索，勿全读日志。']
    used = len(out[0])
    for r in items:
        txt = r['text'].replace('\n', ' ').strip()[:170]
        line = '- [%s] %s · %s' % (r['source'], r['heading'][:48], txt)
        if used + len(line) > budget:
            break
        out.append(line)
        used += len(line)
    return '\n'.join(out)


def cmd_hook_inject(args):
    try:
        payload = json.loads(sys.stdin.read() or '{}')
    except Exception:
        payload = {}
    cwd = payload.get('cwd') or os.getcwd()
    ws, project = resolve(None, cwd)
    # 静默增量索引：只重扫 mtime 变过的 .md，让直接编辑日志的内容也能进本轮注入。
    # 任何异常都吞掉 —— hook 的 stdout 必须只有一行 JSON。
    try:
        if os.path.isdir(memory_dir(ws)):
            index_workspace(ws, project)
    except Exception:
        pass
    txt = inject_text(ws, args.budget)
    out = {'continue': True}
    if txt:
        event = payload.get('hook_event_name') or 'SessionStart'
        # ★ 双客户端契约：Claude Code / Codex 里 systemMessage 只是给用户看的 UI 警告，
        #   真正进模型上下文的是 hookSpecificOutput.additionalContext。
        #   正文只放 additionalContext；systemMessage 留一句短提示，兼容只认它的旧实现。
        out['systemMessage'] = 'vmem: 已注入 %d 字符项目记忆 (%s)' % (len(txt), project)
        out['hookSpecificOutput'] = {
            'hookEventName': event,
            'additionalContext': txt,
        }
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ------------------------------------------------------------------ MCP

TOOLS = [
    {'name': 'memory_search',
     'description': '检索当前项目记忆。返回按相关度排序的片段，每条带 source(文件:行号) 与标题面包屑。用于查历史决策、真值、已踩过的坑，替代逐个读取每日日志。',
     'inputSchema': {'type': 'object', 'properties': {
         'query': {'type': 'string', 'description': '检索词，支持中文短语、代码标识符、字段名'},
         'top_k': {'type': 'integer', 'description': '返回条数，默认 6'},
         'project': {'type': 'string', 'description': '可选，跨项目指定项目 id'}},
         'required': ['query']}},
    {'name': 'memory_add',
     'description': '记一条新记忆。会同时追加到当日 .md 日志（真相源）并增量入索引。用于记录口头约定、临时拍板、没落进文件的决定。',
     'inputSchema': {'type': 'object', 'properties': {
         'text': {'type': 'string'}, 'kind': {'type': 'string', 'description': 'decision/rule/bug/truth/note'},
         'tags': {'type': 'string', 'description': '逗号分隔'}, 'project': {'type': 'string'}},
         'required': ['text']}},
    {'name': 'memory_get',
     'description': '按 id 取回一条记忆的完整原文。',
     'inputSchema': {'type': 'object', 'properties': {
         'id': {'type': 'integer'}, 'project': {'type': 'string'}}, 'required': ['id']}},
    {'name': 'memory_index',
     'description': '增量扫描 .workbuddy/memory/*.md 并入库（按 mtime 跳过未改动文件）。force=true 全量重建。原文永不删除。',
     'inputSchema': {'type': 'object', 'properties': {
         'force': {'type': 'boolean'}, 'project': {'type': 'string'}}}},
    {'name': 'memory_stats',
     'description': '查看当前项目索引状态：块数、文件数、按类型分布、db 大小、是否已启用向量。',
     'inputSchema': {'type': 'object', 'properties': {'project': {'type': 'string'}}}},
]


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def call_tool(name, p):
    if name == 'memory_search':
        return cmd_search(Args(query=p.get('query', ''), k=int(p.get('top_k') or 6),
                               project=p.get('project'), cwd=p.get('cwd')))
    if name == 'memory_add':
        return cmd_add(Args(text=p.get('text', ''), kind=p.get('kind') or 'decision',
                            tags=p.get('tags') or '', project=p.get('project'), cwd=p.get('cwd')))
    if name == 'memory_get':
        return cmd_get(Args(id=int(p.get('id') or 0), project=p.get('project'), cwd=p.get('cwd')))
    if name == 'memory_index':
        return cmd_index(Args(force=bool(p.get('force')), project=p.get('project'), cwd=p.get('cwd')))
    if name == 'memory_stats':
        return cmd_stats(Args(project=p.get('project'), cwd=p.get('cwd')))
    raise KeyError('unknown tool: ' + name)


def mcp_loop():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        method, params = req.get('method', ''), req.get('params') or {}
        rid = req.get('id')
        if method == 'initialize':
            result = {'protocolVersion': MCP_PROTO,
                      'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'vmem', 'version': '0.2.1'}}
        elif method == 'tools/list':
            result = {'tools': TOOLS}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            try:
                data = call_tool(name, args)
                result = {'content': [{'type': 'text',
                                       'text': json.dumps(data, ensure_ascii=False)[:12000]}],
                          'isError': False}
            except Exception as e:
                result = {'content': [{'type': 'text', 'text': '%s: %s' % (type(e).__name__, e)}],
                          'isError': True}
        elif method == 'ping':
            result = {}
        elif method.startswith('notifications/'):
            continue
        else:
            if rid is None:
                continue
            sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid,
                                         'error': {'code': -32601, 'message': 'method not found'}},
                                        ensure_ascii=False) + '\n')
            sys.stdout.flush()
            continue
        if rid is None:
            continue
        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': rid, 'result': result},
                                    ensure_ascii=False) + '\n')
        sys.stdout.flush()
    return 0


# ------------------------------------------------------------------ 入口

def main():
    raw = sys.argv[1:]
    if not raw or raw[0] == 'mcp':
        return mcp_loop()
    ap = argparse.ArgumentParser(description='vmem 通用项目记忆库')
    sub = ap.add_subparsers(dest='cmd')
    for name in ('index', 'rebuild'):
        q = sub.add_parser(name)
        q.add_argument('--project'); q.add_argument('--cwd'); q.add_argument('--force', action='store_true')
        q.add_argument('--all', action='store_true')
    q = sub.add_parser('search')
    q.add_argument('query'); q.add_argument('-k', type=int, default=6)
    q.add_argument('--project'); q.add_argument('--cwd'); q.add_argument('--json', action='store_true')
    q.add_argument('--all', action='store_true')
    q = sub.add_parser('add')
    q.add_argument('--text', required=True); q.add_argument('--kind', default='decision')
    q.add_argument('--tags', default=''); q.add_argument('--project'); q.add_argument('--cwd')
    q = sub.add_parser('get')
    q.add_argument('id', type=int); q.add_argument('--project'); q.add_argument('--cwd')
    q = sub.add_parser('stats')
    q.add_argument('--project'); q.add_argument('--cwd'); q.add_argument('--json', action='store_true')
    q.add_argument('--all', action='store_true')
    q = sub.add_parser('hook-inject')
    q.add_argument('--budget', type=int, default=DEFAULT_BUDGET)
    q = sub.add_parser('mcp')
    args = ap.parse_args(raw)
    if args.cmd == 'mcp':
        return mcp_loop()
    if args.cmd in ('index', 'rebuild'):
        if args.cmd == 'rebuild':
            args.force = True
        r = cmd_index(args)
    elif args.cmd == 'search':
        r = cmd_search(args)
    elif args.cmd == 'add':
        r = cmd_add(args)
    elif args.cmd == 'get':
        r = cmd_get(args)
    elif args.cmd == 'stats':
        r = cmd_stats(args)
    elif args.cmd == 'hook-inject':
        return cmd_hook_inject(args)
    else:
        ap.print_help()
        return 1
    if getattr(args, 'json', False) or args.cmd in ('index', 'rebuild', 'add', 'get', 'stats'):
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print(fmt_results(r, budget=DEFAULT_BUDGET))
    return 0


if __name__ == '__main__':
    sys.exit(main())
