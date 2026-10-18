-- rime.lua · Android/Trime 版 · rime-llm-reranker v8-inproc
--
-- 与 PC (Weasel) v8 同构：击键路径零阻塞、零进程生成、零网络。
-- 差别仅在推理后端：PC 用文件握手驱动外部 sidecar；本版 require("ime")
-- 加载 APK 内的 libime.so（C++ 进程内推理，后台线程打分），
-- 上一键投递 query（微秒级返回），之后的击键 pickup 结果并重排。
-- 数据飞轮/悔改墓碑/AI 开关与 PC 版语义一致，日志落在 Rime 用户目录。

-- ─── 模块加载（进程内 C 推理库） ──────────────────────────
local ime = nil
local _ime_tried = false

local function find_native_dir()
  -- librime-lua 与 librime_jni.so 同进程：从 /proc/self/maps 定位原生库目录
  local f = io.open('/proc/self/maps', 'rb')
  if not f then return nil end
  for line in f:lines() do
    local dir = line:match('%s(/.-)/librime_jni%.so$')
    if dir then f:close(); return dir end
  end
  f:close()
  return nil
end

local function user_dir()
  local d = nil
  pcall(function() d = rime_api and rime_api.get_user_data_dir and rime_api.get_user_data_dir() end)
  return d or '/data/data/com.osfans.trime/files/rime'
end

local USER_DIR = user_dir()
local LOG_PATH = USER_DIR..'/rime_llm.log'
local TRAIN_LOG_PATH = USER_DIR..'/rime_train.jsonl'

local function try_load_ime(cfg)
  if ime or _ime_tried then return end
  _ime_tried = true
  local ok, err = pcall(function()
    local dir = find_native_dir()
    if not dir then error('native dir not found') end
    package.cpath = dir..'/libime.so'
    local m = require('ime')
    local model = cfg and cfg.model ~= '' and cfg.model or 'ime-model.safetensors'
    local mp = USER_DIR..'/'..model
    if not m.load(mp) then error('model load failed: '..mp) end
    ime = m
  end)
  if not ok then
    local lf = io.open(LOG_PATH, 'a')
    if lf then lf:write(os.date('%H:%M:%S')..' IME_LOAD_FAIL '..tostring(err)..'\n'); lf:close() end
    _ime_tried = false   -- 下一次过滤再试（模型可能刚同步进来）
  end
end

-- ─── JSON 编码（与 PC v8 相同） ───────────────────────────
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

-- ─── 配置 ────────────────────────────────────────────────
local function get_config(env)
  local c = env.engine.schema.config
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
    min_margin   = tonumber(gs('llm_reranker/min_margin', '0.03')),
    model        = gs('llm_reranker/model', 'ime-model.safetensors'),
    debug    = gb('llm_reranker/debug_log', false),
    collect_train = gb('llm_reranker/collect_training_data', true),
    collect_user_pairs = gb('llm_reranker/collect_user_pairs', true),
    max_pairwise_negatives = gi('llm_reranker/max_pairwise_negatives', math.max(max_cands - 1, 1)),
  }
end

local _cfg = {}

-- ── 重排候选数旋钮：/sdcard/rime/rerank_knob.txt 写一个整数(2..32)，
--    免部署热调（每秒重读一次，1秒内生效）；文件缺省/非法回落到 yaml 配置值 ──
local _knob_val, _knob_at = nil, 0
local KNOB_PATH = '/sdcard/rime/rerank_knob.txt'
local function rerank_knob()
  local now = os.clock()
  if now - _knob_at < 1.0 then return _knob_val end
  _knob_at = now
  local ok, v = pcall(function()
    local f = io.open(KNOB_PATH, 'r')
    if not f then return nil end
    local s = f:read('*l') or ''
    f:close()
    local n = tonumber(s:match('^%s*(%d+)%s*$'))
    if n and n >= 2 and n <= 32 then return n end
    return nil
  end)
  _knob_val = ok and v or nil
  return _knob_val
end
local function dlog(msg)
  if not _cfg.debug then return end
  local f = io.open(LOG_PATH,'a')
  if f then f:write(os.date('%H:%M:%S')..' '..msg..'\n'); f:close() end
end

-- ─── 训练数据日志（数据飞轮） ─────────────────────────────
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
  -- Trime 内嵌预编辑会在码尾追加插入符 ‸/^（微信等外部应用可见），
  -- 剥掉尾部非编码字节再解析，否则整条解析失败被静默跳过（实测踩坑）
  preedit = preedit:gsub("[^%w%s']+$", '')
  local inline_text, code = preedit:match("^(.-)([A-Za-z0-9' ]+)$")
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
end

-- ─── 音节数估算（与 PC v8 相同） ──────────────────────────
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

-- ─── 模块级状态 ──────────────────────────────────────────
local committed_buffer = ''
local _last_commit_text = ''
local _logged          = false
local _cache      = {}
local _cache_size = 0
local _last_snapshot = nil
local _pending_key = nil   -- 单槽在途（C 层同样只留最新）

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
    _pending_key = nil
    _last_snapshot = nil
  end
end

local function reorder(list, idx)
  if not idx or idx == 1 then return list end
  local r = {list[idx]}
  for i,c in ipairs(list) do if i ~= idx then table.insert(r,c) end end
  return r
end

-- ─── Rime filter 入口 ────────────────────────────────────
function llm_reranker(input, env)
  _cfg = get_config(env)
  local _knob = rerank_knob()
  if _knob then
    _cfg.max_candidates = _knob
    _cfg.max_rerank_pos = _knob
  end

  -- 惰性枚举：只取前 K 个，其余流式直通（移植 PC 650ms->2.5ms 修复；
  -- 原版全量 drain，多音节枚举上万候选需数秒，键盘表现为"无候选"）
  local next_fn, st = input:iter()
  local K = math.max(_cfg.max_cands, _cfg.max_rerank_pos or _cfg.max_cands)
  local all = {}
  for i = 1, K do
    local c = next_fn(st)
    if not c then break end
    table.insert(all, c)
  end
  if not ime then try_load_ime(_cfg) end

  if not _logged and _cfg.debug then
    dlog('v8-inproc started, ime='..tostring(ime ~= nil)..', user_dir='..USER_DIR)
    _logged = true
  end

  update_context_from_history(env)

  local ai_on = true
  pcall(function() ai_on = env.engine.context:get_option('ai_rerank') end)
  local active = _cfg.enabled and ai_on and ime ~= nil

  local preedit = ''
  pcall(function()
    local p = env.engine.context:get_preedit()
    if p then preedit = p.text or '' end
  end)

  local parsed_preedit = parse_preedit(preedit)

  -- 每键决策快照（300ms节流）：一锤定音用，定位后删
  if (os.clock() - (_last_trace or 0)) > 0.3 then
    _last_trace = os.clock()
    dlog(string.format('TRACE active=%s ncands=%d parsed=%s preedit=%q pending=%s',
      tostring(active), #all, tostring(parsed_preedit ~= nil), preedit:sub(1, 24),
      tostring(_pending_key)))
  end

  if not active or #all < 2 then
    if not active and (os.clock() - (_last_off_log or 0)) > 2 then
      _last_off_log = os.clock()
      dlog('INACTIVE: enabled='..tostring(_cfg.enabled)..' ai_on='..tostring(ai_on)..' ime='..tostring(ime ~= nil)..' preedit='..preedit)
    end
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
    _last_snapshot = nil
    for _,c in ipairs(all) do yield(c) end
    for c in next_fn, st do yield(c) end
    return
  end

  local query_preedit = parsed_preedit.code
  local inline_text = parsed_preedit.inline_text
  local context_base = utf8_tail(committed_buffer .. inline_text, 1200)

  -- 九键纯数字输入：按 ~3键/音节 估算；字母专用的简拼/尾码检查不适用
  local digit_input = query_preedit:match('^%d+$') ~= nil
  local syllables = digit_input and math.ceil(#query_preedit / 3)
                    or count_syllables(query_preedit)
  local skip = syllables < _cfg.min_syllables
  dlog(string.format('TRACE skip=%s syl=%d len=%d digit=%s', tostring(skip), syllables,
    #query_preedit, tostring(digit_input)))

  if not skip and query_preedit:len() > _cfg.max_preedit_len then
    skip = true
  end

  if not skip and not digit_input and not is_jianpin(query_preedit)
     and preedit_ends_incomplete(query_preedit) then
    skip = true
  end

  local idx = nil
  if not skip then
    local history_key = utf8_tail(context_base, _cfg.ctx_len)
    local cache_key = (history_key..':'..query_preedit):lower()
    local cached = _cache[cache_key]
    if cached then
      idx = cached.idx
    else
      -- ① 取回上一键投递的结果（C 层微秒级返回，不阻塞）
      if _pending_key then
        local ok, res = pcall(ime.pickup)
        if ok and type(res) == 'table' then
          if res.key == _pending_key then
            local ridx = nil
            if res.idx and res.idx > 1 and res.idx <= _cfg.max_rerank_pos
               and (res.margin or 0) >= _cfg.min_margin then
              ridx = res.idx
              dlog(string.format('RERANK-APPLIED: [%s] idx=%d margin=%.3f',
                                 (res.key or ''):sub(-24), res.idx, res.margin or 0))
            else
              dlog(string.format('RERANK-SKIP: [%s] idx=%s margin=%s',
                                 (res.key or ''):sub(-24), tostring(res.idx), tostring(res.margin)))
            end
            _cache[res.key] = { idx = ridx }
            _cache_size = _cache_size + 1
          end
          _pending_key = nil   -- 匹配则入缓存；陈旧结果直接丢弃
          _pending_misses = 0
        else
          dlog('PICKUP-MISS type='..type(res)..' misses='..tostring((_pending_misses or 0)+1))
          -- 结果尚未就绪：极少数连续未就绪后放弃该请求，避免卡死
          _pending_misses = (_pending_misses or 0) + 1
          if _pending_misses >= 3 then
            _pending_key = nil
            _pending_misses = 0
          end
        end
      end
      if _pending_key == nil then
        local r = _cache[cache_key]
        if r then
          idx = r.idx   -- 同键重复击键：刚回收的就是本键结果
        else
          -- ② 投递新请求后立即直通，结果在之后的击键里生效
          local texts = {}
          for _,c in ipairs(all) do
            if #texts < _cfg.max_cands then table.insert(texts, c.text) end
          end
          local qok = pcall(ime.query, cache_key, history_key, texts)
          dlog('QUERY-OUT ok='..tostring(qok)..' key=..'..cache_key:sub(-20)..' ncands='..#texts)
          _pending_key = cache_key
        end
      end
    end
  end

  local ordered = reorder(all, idx)

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
  for c in next_fn, st do yield(c) end
end

-- ─── init / fini ─────────────────────────────────────────
function llm_reranker_init(env)
  _cfg = get_config(env)
  committed_buffer  = ''
  _last_commit_text = ''
  _logged           = false
  _cache            = {}
  _cache_size       = 0
  _pending_key      = nil
  _last_snapshot    = nil
  try_load_ime(_cfg)
end

function llm_reranker_fini(env)
  committed_buffer  = ''
  _last_commit_text = ''
  _cache            = {}
  _cache_size       = 0
  _pending_key      = nil
  _last_snapshot    = nil
end

-- 悔改信号：上屏后的退格删除 -> retract 墓碑
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
        _retract_watch = nil
        _bs_count = 0
      end
    end
  end
  return 2  -- kNoop
end
