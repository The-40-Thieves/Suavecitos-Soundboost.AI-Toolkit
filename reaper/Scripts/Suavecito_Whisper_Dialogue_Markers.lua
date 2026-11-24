-- @description Suavecito Whisper Dialogue & Lyric Timeline Marker Generator (CUDA / Whisper)
-- @version 1.1.0
-- @author Suavecito SoundBoost + Antigravity

local r = reaper
local TITLE = "Suavecito Whisper Markers"
local is_windows = (package.config:sub(1, 1) == "\\")
local headless = (r.GetExtState("Suavecito", "HeadlessMode") == "1")

local function fail(title, msg)
  r.SetExtState("Suavecito", "LastScriptError", msg, false)
  r.ShowConsoleMsg("[" .. title .. "] ERROR: " .. msg .. "\n")
  if not headless then
    r.ShowMessageBox(msg, title, 0)
  end
end

local function file_exists(path)
  if not path or path == "" then return false end
  local f = io.open(path, "rb")
  if f then
    f:close()
    return true
  end
  return false
end

local function resolve_tool_bin(ext_key, env_var, default_name)
  local ext = r.GetExtState("Suavecito", ext_key)
  if ext and ext ~= "" then return ext end
  local env = os.getenv(env_var)
  if env and env ~= "" then return env end
  if is_windows then
    local up = os.getenv("USERPROFILE") or ""
    if up ~= "" then
      local cmd_path = up .. "\\.local\\bin\\" .. default_name .. ".cmd"
      if file_exists(cmd_path) then return cmd_path end
      local exe_path = up .. "\\.local\\bin\\" .. default_name .. ".exe"
      if file_exists(exe_path) then return exe_path end
    end
    return default_name
  end
  local home = os.getenv("HOME") or ""
  if home ~= "" then
    local local_path = home .. "/.local/bin/" .. default_name
    if file_exists(local_path) then return local_path end
  end
  return default_name
end

local function uses_cmd_shell(bin)
  if not is_windows then return false end
  local lower = bin:lower()
  if lower:match("%.cmd$") or lower:match("%.bat$") then
    return true
  end
  if not bin:find("[/\\]") and not lower:match("%.exe$") then
    return true
  end
  return false
end

local function has_unsafe_arg_chars(val, via_cmd_exe)
  if not val or val == "" then return true end
  if val:find('"', 1, true) or val:find("\r", 1, true) or val:find("\n", 1, true) or val:find("\0", 1, true) then
    return true
  end
  if via_cmd_exe and val:find("%", 1, true) then
    return true
  end
  return false
end

local function build_command(bin, args)
  local via_cmd = uses_cmd_shell(bin)
  if has_unsafe_arg_chars(bin, via_cmd) then
    return nil, "Executable path contains unsafe characters."
  end
  local parts = { '"' .. bin .. '"' }
  for _, arg in ipairs(args) do
    if has_unsafe_arg_chars(arg, via_cmd) then
      return nil, "Command argument contains unsafe characters."
    end
    if arg:match("^[%w_%-%.]+$") then
      table.insert(parts, arg)
    else
      table.insert(parts, '"' .. arg .. '"')
    end
  end
  local joined = table.concat(parts, " ")
  if via_cmd then
    return 'cmd.exe /d /s /c "' .. joined .. '"', nil
  end
  return joined, nil
end

local function run_exec(cmd)
  local timeout_ms = tonumber(r.GetExtState("Suavecito", "ExecTimeoutMs")) or 600000
  if timeout_ms <= 0 then
    timeout_ms = 600000
  end
  local res = r.ExecProcess(cmd, math.floor(timeout_ms))
  if res == nil then
    return false, -1, "ExecProcess failed or timed out"
  end
  local code_str, output = res:match("^(%-?%d+)\r?\n(.*)$")
  if not code_str then
    code_str = res:match("^(%-?%d+)$")
    output = ""
  end
  local exit_code = tonumber(code_str)
  if exit_code == nil then
    return false, -1, res
  end
  if exit_code ~= 0 then
    return false, exit_code, output or ""
  end
  return true, 0, output or ""
end

local function with_suffix(path, suffix)
  local stem = path:match("^(.*)%.[^/\\.%s]+$")
  if stem then return stem .. suffix end
  return path .. suffix
end

local item = r.GetSelectedMediaItem(0, 0)
if not item then
  fail(TITLE, "Please select a vocal or dialogue audio item first.")
  return
end

local take = r.GetActiveTake(item)
if not take or r.TakeIsMIDI(take) then
  fail(TITLE, "Selected item must be an audio take.")
  return
end

local src = r.GetMediaItemTake_Source(take)
local src_path = r.GetMediaSourceFileName(src, "")
if not src_path or src_path == "" then
  fail(TITLE, "Selected audio take has no source file path.")
  return
end

local model = r.GetExtState("Suavecito", "WhisperModel")
if not model or model == "" then
  model = "base"
end
if not model:match("^[%w%._%-]+$") then
  fail(TITLE, "Invalid WhisperModel: " .. model)
  return
end

local item_pos = r.GetMediaItemInfo_Value(item, "D_POSITION")
local srt_path = with_suffix(src_path, ".srt")

local bin = resolve_tool_bin("GameVideoKitBin", "SUAVECITO_GAME_VIDEO_KIT", "game-video-kit")
local cmd, cmd_err = build_command(bin, { "subtitles", src_path, srt_path, "--model", model })
if not cmd then
  fail(TITLE, cmd_err)
  return
end

r.ShowConsoleMsg("[Suavecito Whisper] Transcribing audio (" .. model .. "):\n" .. cmd .. "\n")
local ok, exit_code, output = run_exec(cmd)
if not ok then
  fail(TITLE, string.format("Whisper transcription failed (exit %d): %s", exit_code, output))
  return
end

local f = io.open(srt_path, "rb")
if not f then
  fail(TITLE, "Generated subtitle file could not be opened: " .. srt_path)
  return
end
local content = f:read("*all") or ""
f:close()

content = content:gsub("^\239\187\191", "")
local normalized = content:gsub("\r\n", "\n"):gsub("\r", "\n")

local markers_added = 0
r.Undo_BeginBlock()
for block in (normalized .. "\n\n"):gmatch("(.-)\n\n+") do
  local lines = {}
  for line in block:gmatch("([^\n]+)") do
    table.insert(lines, line)
  end
  local ts_idx = nil
  local h1, m1, s1, ms1 = nil, nil, nil, nil
  for idx, line in ipairs(lines) do
    h1, m1, s1, ms1 = line:match("(%d+):(%d+):(%d+)[,.](%d+)%s*%-%->%s*%d+:%d+:%d+[,.]%d+")
    if h1 then
      ts_idx = idx
      break
    end
  end
  if ts_idx then
    local text_parts = {}
    for j = ts_idx + 1, #lines do
      local trimmed = lines[j]:match("^%s*(.-)%s*$")
      if trimmed ~= "" then
        table.insert(text_parts, trimmed)
      end
    end
    if #text_parts > 0 then
      local text = table.concat(text_parts, " ")
      local t_sec = tonumber(h1) * 3600 + tonumber(m1) * 60 + tonumber(s1) + tonumber(ms1) / (10 ^ #ms1)
      r.AddProjectMarker(0, false, item_pos + t_sec, 0, text, -1)
      markers_added = markers_added + 1
    end
  end
end
r.Undo_EndBlock("Suavecito Whisper Dialogue Markers", -1)
r.UpdateArrange()
r.ShowConsoleMsg(string.format("[Suavecito Whisper] Added %d dialogue markers from %s\n", markers_added, srt_path))
