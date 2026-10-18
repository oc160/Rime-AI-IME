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
local _cfg_cached = false   -- 配置只在首次取一次：每键 12 次 YAML 查找纯属浪费

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
-- ─── 按键耗时测量（临时诊断）：USER_DIR/pred_measure.flag 存在时打 M0/M1/M2 点 ──
local _measure_flag = false
local _measure_at = 0
local function measuring()
  local now = os.clock()
  if _measure_at == 0 or (now - _measure_at) > 2.0 then
    _measure_at = now
    local f = io.open(USER_DIR .. '/pred_measure.flag', 'r')
    _measure_flag = f ~= nil
    if f then f:close() end
  end
  return _measure_flag
end

-- ─── 联想 v2 预取前置声明（实现在下方 PRED 区，filter 先引用） ──
local predict_send, predict_hist_tail, PRED_LAST_SEND, predict_load, predict_has
local PRED_KEY_AT, PRED_KEY_PREV

function llm_reranker(input, env)
  local next_fn, st = input:iter()   -- 惰性迭代器：(函数, 状态)；只取前 K 个，其余流式直通
  local all = {}

  if measuring() then log.info('M1') end

  if not _cfg_cached then
    _cfg = get_config(env)
    _cfg_cached = true
  end
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

  -- ── AI联想 v2：打字途中预取——吃满拼音的 CJK 首候选先异步问下一词，
  --    用户读完候选按空格上屏时结果多半已就绪（实现见下方 PRED 区）。
  --    节流：≥6 键且距上次发送 ≥1s——半截拼音(数据亲/数据区)不触发，
  --    否则打字全程都在跑推理，CPU 抢核导致上屏变慢 ──
  pcall(function()
    local on = true
    on = env.engine.context:get_option('ai_predict')
    if on and #all > 0 then
      predict_load()   -- 幂等：会话首个词也带着完整词表验真词，不错过预取
      local c0 = all[1]
      local t = c0.text or ''
      local b1 = t:byte(1) or 0
      if #t >= 6 and b1 >= 0xE4 and b1 <= 0xE9 then
        local inp = env.engine.context.input or ''
        local pe = c0.preedit or ''
        local chars = math.floor(#t / 3)
        -- 整句模式下 pe==inp 恒真、字数==音节数也常碰巧成立，都会瞄准半截拼词；
        -- 真词校验（predict_has，词表字节扫描）只在停顿分支做——绝不进热路径
        local match = (pe ~= '' and pe == inp) or (chars == count_syllables(inp))
        local now = os.clock()
        -- 只在真停顿时预取（本次按键距前一键 ≥0.45s = 用户在读候选/想词）：
        -- 流式快打期间零查询零推理，CPU 完全让路；停顿处发一条，上屏时结果多半就绪
        local paused = PRED_KEY_PREV > 0 and (PRED_KEY_AT - PRED_KEY_PREV) >= 0.45
        if match and #inp >= 6 and paused and (now - (PRED_LAST_SEND or 0)) >= 0.8
           and predict_has(t) then
          PRED_LAST_SEND = now
          predict_send({ [t] = predict_hist_tail() .. t })
        end
      end
    end
  end)

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

  -- ① 取回 sidecar 结果（仅在有在途请求时读文件；微秒级，非阻塞）
  if next(_pending) ~= nil then
    rpc_pickup()
  end

  local idx = nil
  if not skip then
    local history_key = utf8_tail(context_base, _cfg.ctx_len)
    local cache_key = (history_key..':'..query_preedit):lower()
    local cached = _cache[cache_key]
    if cached then
      idx = cached.idx
      dlog('cache ['..tostring(idx)..'] '..cache_key)
    else
      -- ② 未命中：只在停顿（≥0.45s，用户正盯着候选读）时投递请求后立即直通。
      --    每键都写请求文件毫无意义——拼音每加一字母 cache_key 即变，中间态查询
      --    99% 没用且白付 4 次文件系统调用；停顿处发的这条才是用户会看的结果。
      local paused_now = PRED_KEY_PREV > 0 and (PRED_KEY_AT - PRED_KEY_PREV) >= 0.45
      if paused_now then
        local texts = {}
        for _,c in ipairs(all) do
          if #texts < _cfg.max_cands then table.insert(texts, c.text) end
        end
        rpc_request(cache_key, history_key, query_preedit, texts)
      end
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

-- ─── 上屏后联想（v1 查表底座 + v2 模型置顶）─────────────────────
-- 交互：上屏后自动挂触发码 "'"，候选窗出下一词，数字键选、选完链式预测；
--      打任意字母先静默清掉触发码再正常输入；Esc/退格清除。
--      F4 方案选单「AI联想」开关（ai_predict，默认开）。
-- 性能铁律：词对表绝不能解析成 lua 表对象——librime-lua 每键全堆遍历，
-- 11.3 万行小表 = 每键 +120ms（实测）；必须以单字符串常驻（单对象，GC 不遍历内容），
-- 查询走字节 find + 64 键 LRU 小缓存（只在停顿/挂码时查，不进热路径）。
-- 注：lua_translator 的 init/fini 需用表形式（librime-lua wiki 文档化机制）
local PRED_RAW = nil        -- 词对表原文（单字符串，前置 '\n' 便于统一 find）
local PRED_CACHE = {}       -- word -> { {word=, weight=} } | false(负缓存)
local PRED_CACHE_KEYS = {}  -- LRU 插入序
local PRED_LOADED = false
local PRED_LAST = nil
local PRED_SELF = false

predict_load = function()
  if PRED_LOADED then return end
  PRED_LOADED = true
  local f = io.open(USER_DIR..'/predict_pairs.txt', 'r')
  if not f then log.info('PREDICT: predict_pairs.txt not found') return end
  PRED_RAW = '\n' .. f:read('*all')
  f:close()
  log.info('PREDICT: pairs raw loaded '..#PRED_RAW..' bytes')
end

local function pred_cache_put(w, v)
  PRED_CACHE[w] = v
  table.insert(PRED_CACHE_KEYS, w)
  if #PRED_CACHE_KEYS > 64 then
    PRED_CACHE[table.remove(PRED_CACHE_KEYS, 1)] = nil
  end
end

-- 单键解析：从 pos 起（该行以 \n 开头、键已匹配）收集同键连续行（每键至多 top8）
local function pred_parse_from(w, pos)
  local cands = {}
  local p = pos + 1
  for _ = 1, 8 do
    local le = PRED_RAW:find('\n', p, true)
    le = le or (#PRED_RAW + 1)
    local line = PRED_RAW:sub(p, le - 1)
    local k2, wd, cnt = line:match('^([^\t]+)\t([^\t]+)\t(%d+)$')
    if not k2 or k2 ~= w then break end
    table.insert(cands, { word = wd, weight = tonumber(cnt) or 0 })
    if le > #PRED_RAW then break end
    p = le + 1
  end
  return cands
end

local function predict_lookup(text)
  if not text or text == '' or not PRED_RAW then return nil end
  local hit = PRED_CACHE[text]
  if hit ~= nil then return hit or nil end
  -- 级联：整词 → 尾 6/5/4/3/2 字（字节安全：3 字节/汉字）
  local found = nil
  local tries = { text }
  for l = math.min(6, math.floor(#text / 3)), 2, -1 do
    table.insert(tries, text:sub(-l * 3))
  end
  for _, w in ipairs(tries) do
    local pos = PRED_RAW:find('\n' .. w .. '\t', 1, true)
    if pos then
      local cands = pred_parse_from(w, pos)
      if #cands > 0 then found = cands break end
    end
  end
  pred_cache_put(text, found or false)
  return found
end

predict_has = function(w)
  return predict_lookup(w) ~= nil
end

-- ── v2：模型结果缓存 + 异步请求（与 scripts/pred_sidecar.py 文件握手）──
-- pred_req.txt：lua 覆盖写，每行 "词<Tab>上下文"；pred_resp.txt：副驾追加
-- "词<Tab>模型下一词<Tab>秒"，挂码时整读建映射。查不到/断服一律查表兜底。
local PRED_MODEL = {}   -- word -> 模型下一词
local PRED_SENT  = {}   -- word -> true（本会话已请求过）
local PRED_SHOW  = nil  -- 当前挂码展示序列 { {word=, src='model'|'table'} }
PRED_LAST_SEND = 0      -- 打字途中预取节流（os.clock 秒，前置声明于 filter 前）

predict_hist_tail = function()
  return utf8_tail(committed_buffer, 90)
end

local function predict_model_read()
  local f = io.open(USER_DIR .. '/pred_resp.txt', 'rb')
  if not f then return end
  local body = f:read(128 * 1024)
  f:close()
  if not body or body == '' then return end
  PRED_MODEL = {}
  for line in body:gmatch('[^\r\n]+') do
    local w, p = line:match('^([^\t]+)\t([^\t]+)')
    if w and p then PRED_MODEL[w] = p end
  end
end

predict_send = function(map)
  local lines = {}
  for w, c in pairs(map) do
    if w and c and not PRED_SENT[w] and PRED_MODEL[w] == nil then
      PRED_SENT[w] = true
      table.insert(lines, w .. '\t' .. c)
    end
    if #lines >= 4 then break end
  end
  if #lines == 0 then return end
  atomic_write(USER_DIR .. '/pred_req.txt', table.concat(lines, '\n') .. '\n')
end

local PRED_AT = 0
local PRED_SEEN = nil          -- 上次 hook 已处理过的上屏文本：同一个词再来 update 必是清除事件，绝不重挂
local PRED_TTL = 6.0           -- 挂码有效期（秒）：超时后任何键先清再透传（防点击别处后的隐形残留吃数字/空格）
PRED_KEY_AT, PRED_KEY_PREV = 0, 0  -- 按键节奏（processor 全键记录；前置声明于 filter 之前）
local PRED_GAPS = {}           -- 最近 10 次有效按键间隔（秒），节奏门自适应用
local PRED_CHAIN = false       -- 本次上屏来自数字选中（链式联想中），hook 消费后复位
local function predict_flow_threshold()
  -- 自适应节奏门槛：近 10 键间隔中位数 ×2.5，夹在 [0.35, 0.9]——
  -- 用户打得多快门槛贴多紧；慢打者的"阅读停顿"远长于自身节奏，仍能挂码
  local n = #PRED_GAPS
  if n == 0 then return 0.35 end
  local s = {}
  for _, g in ipairs(PRED_GAPS) do table.insert(s, g) end
  table.sort(s)
  local med = s[math.ceil(n / 2)]
  return math.max(0.35, math.min(2.5 * med, 0.90))
end
local function predict_hook(ctx)
  if measuring() then log.info('M2') end
  if PRED_SELF then return end
  -- 只在引擎真正空闲时挂码（update_notifier 在 commit 清理之后才轮到我们，
  -- 否则 commit_notifier 里推入的码会被引擎的清空动作抹掉）
  local composing = false
  pcall(function() composing = ctx:is_composing() end)
  if composing or (ctx.input and ctx.input ~= '') then return end
  local on = true
  pcall(function() on = ctx:get_option('ai_predict') end)
  if not on then return end
  local latest = nil
  pcall(function()
    local hist = ctx.commit_history
    if hist and not hist:empty() then latest = hist:back().text end
  end)
  if not latest or latest == '' then return end
  -- 关键守卫：只有出现【新】上屏才挂码。weasel 失焦/点击别处会 ClearComposition，
  -- 该清除也触发 update_notifier，此时上屏历史没变——若重挂，' 会立即复活（分字符去不掉）。
  if latest == PRED_SEEN then return end
  PRED_SEEN = latest
  -- 只有以 CJK 汉字开头的上屏才挂码：排除 ASCII 标点/数字、全角符号（首字节 0xEF，如（）、emoji
  local b1 = latest:byte(1) or 0
  if b1 < 0xE4 or b1 > 0xE9 then PRED_LAST = nil PRED_CHAIN = false return end
  -- 节奏门（自适应）：本次上屏间隔小于 近10键中位间隔×2.5（夹在[0.35,0.9]s）属流式输入，
  -- 用户不看联想——不挂码不查询不建组段。数字链式选中（PRED_CHAIN）豁免。
  local flowed = (not PRED_CHAIN) and PRED_KEY_PREV > 0
                 and (PRED_KEY_AT - PRED_KEY_PREV) < predict_flow_threshold()
  if flowed then PRED_LAST = latest PRED_CHAIN = false return end
  PRED_LAST = latest
  local was_chain = PRED_CHAIN   -- 本次上屏是否来自数字选中（决定链式预取深度）
  PRED_CHAIN = false
  predict_load()
  predict_model_read()
  -- 展示序列：模型下一词（有则置顶）+ 查表词补尾，去重、跳过原词，最多8个
  local cands = predict_lookup(latest)
  PRED_SHOW = {}
  local seen = {}
  local mp = PRED_MODEL[latest]
  if mp and mp ~= latest and #mp >= 3 and #mp <= 24 and (mp:byte(1) or 0) >= 0xE4 then
    table.insert(PRED_SHOW, { word = mp, src = 'model' })
    seen[mp] = true
  end
  if cands then
    for _, c in ipairs(cands) do
      if #PRED_SHOW >= 8 then break end
      if c.word ~= latest and not seen[c.word] then
        table.insert(PRED_SHOW, { word = c.word, src = 'table' })
        seen[c.word] = true
      end
    end
  end
  if #PRED_SHOW == 0 then return end   -- 模型与查表都空：不挂码
  -- 上下文尾巴：上屏历史缓冲 + 刚上屏的词（防 filter 尚未把它计入缓冲）
  local ctx_base = committed_buffer
  if ctx_base:sub(-#latest) ~= latest then ctx_base = ctx_base .. latest end
  local tail = utf8_tail(ctx_base, 90)
  -- 链式预取：平时只预取第1名（省推理不抢核）；数字链式联想进行中才预取前3名
  local q = {}
  local topn = was_chain and 3 or 1
  for i = 1, math.min(topn, #PRED_SHOW) do
    q[PRED_SHOW[i].word] = tail .. PRED_SHOW[i].word
  end
  predict_send(q)
  PRED_AT = os.clock()
  PRED_SELF = true
  local ok = pcall(function() ctx:push_input("'") end)
  PRED_SELF = false
  log.info('PREDICT: arm after "'..latest..'" n='..#PRED_SHOW..
    (mp and (' model='..mp) or '')..' push='..tostring(ok))
end

predictor_translator = {
  init = function(env)
    predict_load()
    env.pred_conn = env.engine.context.update_notifier:connect(predict_hook)
    log.info('PREDICT: translator init, notifier connected')
  end,
  func = function(input, seg, env)
    if input ~= "'" then return end
    local on = true
    pcall(function() on = env.engine.context:get_option('ai_predict') end)
    if not on or not PRED_LAST then return end
    if PRED_SHOW then
      for _, item in ipairs(PRED_SHOW) do
        if item.word ~= PRED_LAST then
          yield(Candidate('prediction', seg.start, seg._end, item.word,
                          item.src == 'model' and '智' or '联'))
        end
      end
      return
    end
    local cands = predict_lookup(PRED_LAST)
    if not cands then return end
    for _, c in ipairs(cands) do
      if c.word ~= PRED_LAST then
        yield(Candidate('prediction', seg.start, seg._end, c.word, '联'))
      end
    end
  end,
  fini = function(env)
    if env.pred_conn then env.pred_conn:disconnect() end
  end,
}

predictor_processor = {
  func = function(key_event, env)
    local ctx = env.engine.context
    local kc = key_event.keycode
    -- 全键记录节奏（不含释放键）：hook 用"上屏键与前一键的间隔"自适应判定流式输入
    local rel = false
    pcall(function() rel = key_event:release() end)
    if not rel then
      if measuring() then log.info(string.format('M0 0x%X', key_event.keycode)) end
      PRED_KEY_PREV = PRED_KEY_AT
      PRED_KEY_AT = os.clock()
      if PRED_KEY_PREV > 0 then
        local g = PRED_KEY_AT - PRED_KEY_PREV
        if g <= 3.0 then   -- 超长间隔是走神/离开，不算打字节奏
          table.insert(PRED_GAPS, g)
          if #PRED_GAPS > 10 then table.remove(PRED_GAPS, 1) end
        end
      end
    end
    local composing = false
    pcall(function() composing = ctx:is_composing() end)
    if composing and ctx.input == "'" then
      local shift, ctrl, alt, release = false, false, false, false
      pcall(function()
        shift = key_event:shift(); ctrl = key_event:ctrl() or key_event:alt()
        release = key_event:release()
      end)
      if release then return 2 end
      if kc == 0xFF1B or kc == 0xFF08 then      -- Esc / Backspace：清联想并吞键
        ctx:clear()
        return 1  -- kAccepted
      end
      -- 纯数字(无修饰)：选联想候选，但仅挂码后 TTL 内有效——
      -- 点击别处后 rime 会话可能残留隐形 "'"（TSF 侧已清但没通知引擎），超时的数字先清再透传
      local plain_digit = kc >= 0x30 and kc <= 0x39 and not shift and not ctrl
      local stale = PRED_AT == 0 or (os.clock() - PRED_AT) > PRED_TTL
      if plain_digit and not stale then
        PRED_CHAIN = true   -- 本次上屏将来自数字选中：hook 据此豁免节奏门+加深链式预取
        return 2  -- kNoop：交给 selector 按数字选联想
      end
      -- 其余任何键（字母/Shift符号/方向键/回车/Tab/空格/超时数字…）：
      -- 先清联想再放行本键，绝不让 "'" 被兜底上屏或在其中移光标。
      -- 空格不再快选第一候选：顶掉后空格正常落到应用（打出真实空格）。
      ctx:clear()
      return 2  -- kNoop：本键随后正常处理（符号照出、回车照换行、空格照打）
    end
    return 2  -- kNoop
  end,
}
