-- rime.lua
-- rime-llm-reranker · v8 异步文件握手版 · Windows 移植（Weasel + llama.cpp llama-server）
--
-- v8 根治设计（2026-10-13，T10 冻结事件后）：
--   击键路径上零进程生成、零网络、零等待——只剩微秒级文件读写。
--   本键：把查询请求原子写入文件 → 直通返回（不阻塞）
--   下一键：读 sidecar 的结果文件 → 应用重排（过期/缺失结果自动直通）
--   sidecar 进程由部署方启动，绝不进击键路径；它死了只是不重排，打字无感。
--   事件背景：v7 及之前把同步 curl 查询（150-500ms）和方案冷加载压在宿主应用
--   UI 线程的同步 COM 上，实体打字触发 explorer/ZCode AppHang（详见
--   reports/incident_2026-10-13_t10_typing.md）。
--
-- 历史修复保留：v6 的 Windows 路径/编码适配；v7 的 enabled:false 数据飞轮快照、
--   bool 配置陷阱修复、候选解析尾标点剥离。

-- ─── Windows 用户目录 ─────────────────────────────────────
local _appdata = os.getenv('APPDATA') or os.getenv('USERPROFILE') or '.'
local USER_DIR = _appdata..'/Rime'
-- RPC 目录：优先用配置显式指定的 rpc_dir；否则用户目录纯 ASCII 时用
-- USER_DIR/llm_rpc，再否则回退 C:/Windows/Temp（中文路径 + ANSI 代码页不可靠）
local RPC_DIR = nil   -- 在 get_config 后确定（见 llm_reranker）

-- ─── JSON 编码 ───────────────────────────────────────────
local function json_encode(v)
  local ESC = {['"']='\\"',['\\']='\\\\', ['\n']='\\n',['\r']='\\r',['\t']='\\t'}
  local function esc(s)
    s = s:gsub('["\\\n\r\t]', ESC)
    s = s:gsub('[\x00-\x1f]', function(c) return string.format('\\u%04x',c:byte()) end)
    return s
  end
  local function is_arr(t)
    local n=0
    for k in pairs(t) do
      if type(k)~='number' or k~=math.floor(k) or k<1 then return false end
      n=n+1
    end
    return n==#t
  end
  local function enc(x, d)
    d=d or 0
    local t=type(x)
    if     t=='nil'     then return 'null'
    elseif t=='boolean' then return x and 'true' or 'false'
    elseif t=='number'  then
      if x~=x or math.abs(x)==math.huge then return 'null' end
      if x==math.floor(x) and math.abs(x)<1e15 then return string.format('%d',x) end
      return string.format('%.17g',x)
    elseif t=='string' then return '"'..esc(x)..'"'
    elseif t=='table'  then
      if is_arr(x) then
        local p={} for i=1,#x do p[i]=enc(x[i],d+1) end
        return '['..table.concat(p,',')..']'
      else
        local p={}
        for k,val in pairs(x) do
          if type(k)=='string' then
            table.insert(p, '"'..esc(k)..'":'..enc(val,d+1))
          end
        end
        return '{'..table.concat(p,',')..'}'
      end
    end
    return 'null'
  end
  local ok, r = pcall(enc, v)
  return ok and r or nil
end

-- ─── 配置读取 ────────────────────────────────────────────
local function get_config(env)
  local c = env.engine.schema.config
  -- bool 读取不能用 "v~=nil and v or d"——配置值为 false 时会错误回退到默认值
  local function gb(k,d) local v=c:get_bool(k); if v==nil then return d end return v end
  local function gs(k,d) return c:get_string(k) or d end
  local function gi(k,d) return c:get_int(k)    or d end
  local max_cands = gi('llm_reranker/max_candidates', 5)
  return {
    enabled      = gb('llm_reranker/enabled', true),
    max_cands    = max_cands,
    ctx_len      = gi('llm_reranker/context_length', 60),
    min_syllables   = gi('llm_reranker/min_syllables', 2),
    max_preedit_len = gi('llm_reranker/max_preedit_len', 10),
    max_rerank_pos  = gi('llm_reranker/max_rerank_pos', 5),
    resp_fresh_s    = gi('llm_reranker/resp_fresh_seconds', 3),
    debug    = gb('llm_reranker/debug_log', false),
    rpc_dir  = gs('llm_reranker/rpc_dir', ''),
    collect_train = gb('llm_reranker/collect_training_data', true),
    collect_user_pairs = gb('llm_reranker/collect_user_pairs', true),
    collect_llm_pairs = gb('llm_reranker/collect_llm_pairs', false),
    max_pairwise_negatives = gi('llm_reranker/max_pairwise_negatives', math.max(max_cands - 1, 1)),
  }
end

local _cfg = {}

-- ─── 调试日志 ────────────────────────────────────────────
local LOG_PATH = USER_DIR..'/rime_llm.log'
local function dlog(msg)
  if not _cfg.debug then return end
  local f = io.open(LOG_PATH,'a')
  if f then f:write(os.date('%H:%M:%S')..' '..msg..'\n'); f:close() end
end

-- ─── 训练数据日志（数据飞轮，与 v7 相同） ──────────────────
local TRAIN_LOG_PATH = USER_DIR..'/rime_train.jsonl'
local _train_seq = 0
local _last_user_seq = 0
local _retract_watch = nil
local _bs_count = 0

local function utf8_tail(s, max_bytes)
  if #s <= max_bytes then return s end
  local start = #s - max_bytes + 1
  while start <= #s and s:byte(start) >= 0x80 and s:byte(start) <= 0xBF do
    start = start + 1
  end
  return s:sub(start)
end

local function unique_texts(texts)
  local result, seen = {}, {}
  for _, text in ipairs(texts or {}) do
    if text and text ~= '' and not seen[text] then
      table.insert(result, text); seen[text] = true
    end
  end
  return result
end

local function parse_preedit(preedit)
  if not preedit or preedit == '' then return nil end
  local inline_text, code = preedit:match("^(.-)([A-Za-z' ]+)$")
  if not code or code == '' then return nil end
  return { raw = preedit, inline_text = inline_text or '', code = code }
end

local function log_train_pair(context, pinyin, positive, negative, source, meta)
  if not _cfg.collect_train then return end
  if not positive or not negative or positive == negative then return end
  if positive == '' or negative == '' then return end
  _train_seq = _train_seq + 1
  local payload = { ts = os.time(), seq = _train_seq,
                    context = context, pinyin = pinyin,
                    positive = positive, negative = negative, source = source }
  if meta then for k, v in pairs(meta) do payload[k] = v end end
  local record = json_encode(payload)
  if not record then return end
  local f = io.open(TRAIN_LOG_PATH, 'a')
  if f then f:write(record..'\n'); f:close() end
  if source == 'user_accept' or source == 'user_select' then
    _last_user_seq = _train_seq
  end
end

local function index_of(list, word)
  for i, w in ipairs(list) do if w == word then return i end end
  return nil
end

local function selected_candidate_text(snap, committed)
  if not snap or not committed then return committed end
  local inline_text = snap.inline_text or ''
  if inline_text ~= '' and committed:sub(1, #inline_text) == inline_text then
    local suffix = committed:sub(#inline_text + 1)
    if suffix ~= '' then return suffix end
  end
  return committed
end

local function log_user_selection_pairs(snap, selected)
  if not _cfg.collect_train or not _cfg.collect_user_pairs then return end
  if not snap or not snap.ordered_texts or #snap.ordered_texts < 2 then return end
  if not selected or selected == '' then return end
  local shown_candidates = unique_texts(snap.ordered_texts)
  if #shown_candidates < 2 then return end
  local selected_pos = index_of(shown_candidates, selected)
  if not selected_pos then return end
  local source = selected_pos == 1 and 'user_accept' or 'user_select'
  local logged = 0
  for pos, negative in ipairs(shown_candidates) do
    local is_comparable = false
    if selected_pos == 1 then is_comparable = pos > 1
    else is_comparable = pos < selected_pos end
    if is_comparable and negative ~= selected then
      log_train_pair(snap.context, snap.pinyin, selected, negative, source, {
        positive_pos = selected_pos, negative_pos = pos,
        shown_candidates = shown_candidates, label_quality = 'user',
      })
      logged = logged + 1
      if logged >= _cfg.max_pairwise_negatives then break end
    end
  end
  if logged > 0 then
    dlog(string.format('USER_PAIR: %s pos=%d pairs=%d pinyin=%s',
      selected, selected_pos, logged, snap.pinyin))
  end
end

-- ─── 音节数估算 ──────────────────────────────────────────
local INITIALS = {
  'zh','ch','sh','b','p','m','f','d','t','n','l','g','k','h',
  'j','q','x','z','c','s','r','y','w',
}
local function count_syllables(preedit)
  if not preedit or preedit == '' then return 0 end
  local s = preedit:lower()
  local count, i = 0, 1
  while i <= #s do
    local matched = false
    for _, ini in ipairs(INITIALS) do
      if s:sub(i, i+#ini-1) == ini then
        count = count + 1; i = i + #ini
        while i <= #s and s:sub(i,i):match('[aeiouüv]') do i = i + 1 end
        if s:sub(i,i+1) == 'ng' then i = i + 2
        elseif s:sub(i,i) == 'n' then i = i + 1 end
        matched = true; break
      end
    end
    if not matched then
      if s:sub(i,i):match('[aeiouüv]') then
        count = count + 1
        while i <= #s and s:sub(i,i):match('[aeiouüvng]') do i = i + 1 end
      else
        i = i + 1
      end
    end
  end
  return count
end

-- ─── 候选词匹配（与 bench 解析语义一致） ──────────────────
local function clean_word(w)
  w = w:gsub('^[%d①②③%.、。%s]+', '')
  w = w:gsub('[%d①②③%.、。,，;；!！%s]+$', '')
  return w
end
local function find_in_texts(texts, word)
  word = clean_word(word or '')
  if word == '' then return nil end
  for i, t in ipairs(texts) do if t == word then return i end end
  return nil
end

-- ─── 模块级状态（必须在引用它的函数之前声明） ──────────────
local committed_buffer = ''local _last_commit_text = ''
local _logged          = false
local _cache      = {}
local _cache_size = 0
local CACHE_MAX   = 300
local _last_snapshot = nil

-- ─── 异步 RPC：文件握手（击键路径的全部开销 = 这两个函数） ──
local _pending = {}   -- id -> {key, ctx, texts}
local _req_seq  = 0
local _miss     = 0   -- sidecar 无响应计数（仅观测，不影响打字）

local function atomic_write(path, body)
  local tmp = path..'.tmp'
  local f = io.open(tmp, 'wb')
  if not f then return false end
  f:write(body); f:close()
  os.remove(path)                      -- Windows 的 rename 不覆盖已存在目标
  return os.rename(tmp, path) ~= nil
end

-- 读一次结果文件；新鲜且属于某个在途请求 → 写入缓存。非阻塞，微秒级。
local function rpc_pickup()
  local f = io.open(RPC_DIR..'/resp.json', 'rb')
  if not f then return end
  local body = f:read('*all'); f:close()
  if not body or body == '' then return end
  local id   = body:match('"id"%s*:%s*"([^"]+)"')
  local word = body:match('"word"%s*:%s*"(.-)"')
  local ts   = tonumber(body:match('"ts"%s*:%s*(%d+)') or '0')
  if not id or not ts or (os.time() - ts) > _cfg.resp_fresh_s then return end
  local p = _pending[id]
  if not p then return end
  _pending[id] = nil
  local idx = find_in_texts(p.texts, word or '')
  if idx and idx > _cfg.max_rerank_pos then idx = nil end
  _cache[p.key] = { idx = idx or false, texts = p.texts, ctx = p.ctx }
  _cache_size = _cache_size + 1
  dlog(string.format('RPC_IN: %s → [%s] idx=%s (pending=%d)',
    p.key, word or 'nil', tostring(idx), 0))
end

-- 投递查询请求（写文件即返回，不等待）。同一 cache_key 只在途一个。
local function rpc_request(cache_key, ctx, preedit, texts)
  for _, p in pairs(_pending) do
    if p.key == cache_key then return end
  end
  _req_seq = _req_seq + 1
  local id = string.format('%d.%d', os.time(), _req_seq)
  local body = json_encode({ id = id, ctx = ctx, pinyin = preedit, cands = texts })
  if not body then return end
  if atomic_write(RPC_DIR..'/req.json', body) then
    _pending[id] = { key = cache_key, ctx = ctx, texts = texts }
    dlog('RPC_OUT: '..id..' '..preedit)
  else
    _miss = _miss + 1
    if _miss % 20 == 1 then dlog('RPC write failed (miss='.._miss..')') end
  end
end

-- ─── 拼音完整性判断 ──────────────────────────────────────
local function is_jianpin(preedit)
  return preedit:lower():match('^[bcdfghjklmnpqrstxyz]+$') ~= nil
end
local COMPLETE_ENDINGS = {
  'ang','eng','ing','ong','ung','ian','uan','uen','van',
  'an','en','in','un','vn','ng','er',
}
local function preedit_ends_incomplete(preedit)
  if not preedit or preedit == '' then return true end
  local s = preedit:lower()
  if is_jianpin(s) then return false end
  if s:sub(-1):match('[aeiouüv]') then return false end
  for _, ending in ipairs(COMPLETE_ENDINGS) do
    if #s >= #ending and s:sub(-#ending) == ending then return false end
  end
  return true
end

-- ─── 主动收集上下文（代替回调） ──────────────────────────
local function update_context_from_history(env)
  local latest = nil
  pcall(function()
    local hist = env.engine.context.commit_history
    if hist and not hist:empty() then
      local entry = hist:back()
      if entry and entry.text then latest = entry.text end
    end
  end)
  if latest and latest ~= '' and latest ~= _last_commit_text then
    local snap = _last_snapshot
    local commit_text = latest
    if snap and snap.inline_text and snap.inline_text ~= '' then
      if latest:sub(1, #snap.inline_text) ~= snap.inline_text then
        commit_text = snap.inline_text .. latest
      end
    end
    committed_buffer = utf8_tail(committed_buffer .. commit_text, 1200)
    _last_commit_text = latest
    dlog('COMMIT: '..commit_text..' | buf_len='..#committed_buffer)
    local seq_before = _last_user_seq
    log_user_selection_pairs(snap, selected_candidate_text(snap, latest))
    if _last_user_seq ~= seq_before then
      local chars = math.max(1, math.ceil(#commit_text / 3))
      _retract_watch = { word = commit_text, ts = os.time(),
                         seq = _last_user_seq, chars = chars }
      _bs_count = 0
    end
    _cache = {}
    _cache_size = 0
    _pending = {}
    _last_snapshot = nil
  end
end

-- ─── 重排序 ──────────────────────────────────────────────
local function reorder(list, idx)
  if not idx or idx == 1 then return list end
  local r = {list[idx]}
  for i,c in ipairs(list) do if i ~= idx then table.insert(r,c) end end
  return r
end

-- ─── Rime filter 入口 ────────────────────────────────────
function llm_reranker(input, env)
  local next_fn, st = input:iter()   -- 惰性迭代器：(函数, 状态)；只取前 K 个，其余流式直通
  local all = {}

  _cfg = get_config(env)
  if not RPC_DIR then
    RPC_DIR = _cfg.rpc_dir ~= '' and _cfg.rpc_dir
      or (USER_DIR:match('^[%w\\/:_%-%. ]+$') and USER_DIR..'/llm_rpc' or 'C:/Windows/Temp/llm_rpc')
  end

  if not _logged and _cfg.debug then
    dlog('v8-async started, rpc dir: '..RPC_DIR)
    _logged = true
  end

  local K = math.max(_cfg.max_cands, _cfg.max_rerank_pos or _cfg.max_cands)
  for i = 1, K do
    local c = next_fn(st)
    if not c then break end
    table.insert(all, c)
  end

  update_context_from_history(env)

  -- switcher 开关（方案选单 F4/Ctrl+` 里「AI重排」可随时切，默认开）：
  -- enabled 配置管总闸，运行时开关管用户手感——两者都开才工作
  local ai_on = true
  pcall(function() ai_on = env.engine.context:get_option('ai_rerank') end)
  local active = _cfg.enabled and ai_on

  local preedit = ''
  pcall(function()
    local p = env.engine.context:get_preedit()
    if p then preedit = p.text or '' end
  end)

  local parsed_preedit = parse_preedit(preedit)

  if not active or #all < 2 then
    -- enabled:false 也要喂快照给数据飞轮：只记录展示顺序，零开销
    if _cfg.collect_train and parsed_preedit and #all >= 2 then
      local shown = {}
      for i,c in ipairs(all) do
        if i <= _cfg.max_cands then table.insert(shown, c.text) end
      end
      _last_snapshot = {
        context       = utf8_tail(committed_buffer .. parsed_preedit.inline_text, _cfg.ctx_len),
        pinyin        = parsed_preedit.code,
        inline_text   = parsed_preedit.inline_text,
        ordered_texts = shown,
      }
    end
    for _,c in ipairs(all) do yield(c) end
    for c in next_fn, st do yield(c) end
    return
  end

  if not parsed_preedit then
    dlog('SKIP: unsupported preedit '..preedit)
    _last_snapshot = nil
    for _,c in ipairs(all) do yield(c) end
    for c in next_fn, st do yield(c) end
    return
  end

  local query_preedit = parsed_preedit.code
  local inline_text = parsed_preedit.inline_text
  local context_base = utf8_tail(committed_buffer .. inline_text, 1200)

  local syllables = count_syllables(query_preedit)
  local skip = syllables < _cfg.min_syllables
  if skip then dlog('SKIP: syllables '..syllables) end

  if not skip and query_preedit:len() > _cfg.max_preedit_len then
    skip = true
    dlog('SKIP: preedit too long')
  end

  if not skip and not is_jianpin(query_preedit)
     and preedit_ends_incomplete(query_preedit) then
    skip = true
    dlog('SKIP: incomplete pinyin tail '..query_preedit)
  end

  -- ① 先取回 sidecar 结果（微秒级，非阻塞）
  rpc_pickup()

  local idx = nil
  if not skip then
    local history_key = utf8_tail(context_base, _cfg.ctx_len)
    local cache_key = (history_key..':'..query_preedit):lower()
    local cached = _cache[cache_key]
    if cached then
      idx = cached.idx
      dlog('cache ['..tostring(idx)..'] '..cache_key)
    else
      -- ② 未命中：投递请求后立即直通。结果在之后的击键里生效
      local texts = {}
      for _,c in ipairs(all) do
        if #texts < _cfg.max_cands then table.insert(texts, c.text) end
      end
      rpc_request(cache_key, history_key, query_preedit, texts)
    end
  end

  local top = all   -- all 已截断为前 K 个；K >= max_cands 时 top 语义不变

  local ordered = reorder(top, idx)

  if _cfg.collect_train then
    local ordered_texts = {}
    for _, c in ipairs(ordered) do table.insert(ordered_texts, c.text) end
    _last_snapshot = {
      context       = utf8_tail(context_base, _cfg.ctx_len),
      pinyin        = query_preedit,
      inline_text   = inline_text,
      ordered_texts = ordered_texts,
    }
  end

  for _,c in ipairs(ordered) do yield(c) end
  for c in next_fn, st do yield(c) end   -- 第 K 个之后的候选：零处理直通
end

-- ─── init / fini ─────────────────────────────────────────
function llm_reranker_init(env)
  _cfg = get_config(env)
  committed_buffer  = ''
  _last_commit_text = ''
  _logged           = false
  _cache            = {}
  _cache_size       = 0
  _pending          = {}
  _last_snapshot    = nil
  dlog('INIT done (v8-async)')
end

function llm_reranker_fini(env)
  committed_buffer  = ''
  _last_commit_text = ''
  _cache            = {}
  _cache_size       = 0
  _pending          = {}
  _last_snapshot    = nil
end

-- 废弃的按键处理器（保留空函数）
function llm_context_tracker(key_event, env)
  return 2  -- kNoop
end

-- 悔改信号：上屏后的退格删除 -> retract 墓碑（训练侧按 ref_seq 丢弃该 pair）
local KEY_BACKSPACE = 0xFF08

function llm_retract_tracker(key_event, env)
  if key_event.keycode == KEY_BACKSPACE and not key_event:release() then
    local composing = false
    pcall(function() composing = env.engine.context:is_composing() end)
    if composing then
      _bs_count = 0
    elseif _retract_watch and os.time() - _retract_watch.ts <= 5 then
      _bs_count = _bs_count + 1
      if _bs_count >= _retract_watch.chars then
        local rec = json_encode({ type = 'retract', ref_seq = _retract_watch.seq,
                                  word = _retract_watch.word, ts = os.time() })
        if rec then
          local f = io.open(TRAIN_LOG_PATH, 'a')
          if f then f:write(rec..string.char(10)); f:close() end
        end
        dlog('RETRACT: '.._retract_watch.word..' (seq '.._retract_watch.seq..')')
        _retract_watch = nil
        _bs_count = 0
      end
    end
  end
  return 2  -- kNoop
end
