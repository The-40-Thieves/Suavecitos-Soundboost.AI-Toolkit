-- @description Suavecito AI Stem Splitter (HTDemucs / BS-RoFormer -> Auto SoundBoost Folder Bus Import)
-- @version 2.1.0
-- @author Suavecito (The 40 Thieves)

local r = reaper
local TITLE = "Suavecito AI Stem Split"
local is_windows = (package.config:sub(1, 1) == "\\")
local sep = is_windows and "\\" or "/"
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

local item = r.GetSelectedMediaItem(0, 0)
if not item then
  fail(TITLE, "Please select an audio item in the timeline first.")
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

local engine = r.GetExtState("Suavecito", "StemSplitEngine")
if not engine or engine == "" then
  engine = "demucs"
end
if not engine:match("^[%w_%-]+$") then
  fail(TITLE, "Invalid StemSplitEngine: " .. engine)
  return
end

local dir, fname = src_path:match("^(.*)[/\\]([^/\\]+)$")
if not dir or dir == "" then
  dir = "."
  fname = src_path
end
local base_name = fname:match("^(.+)%.[^.]+$") or fname
local out_dir = dir .. sep .. "Stems_AI"

local bin = resolve_tool_bin("VocalMusicKitBin", "SUAVECITO_VOCAL_MUSIC_KIT", "vocal-music-kit")
local cmd, cmd_err = build_command(bin, { "stems", src_path, out_dir, "--engine", engine })
if not cmd then
  fail(TITLE, cmd_err)
  return
end

r.ShowConsoleMsg("[Suavecito AI] Splitting stems (" .. engine .. "):\n" .. cmd .. "\n")
local ok, exit_code, output = run_exec(cmd)
if not ok then
  fail(TITLE, string.format("Stem split failed (exit %d): %s", exit_code, output))
  return
end

local candidates = {}
if engine == "demucs" then
  table.insert(candidates, out_dir .. sep .. "htdemucs" .. sep .. base_name)
end
table.insert(candidates, out_dir .. sep .. engine .. sep .. base_name)
table.insert(candidates, out_dir)

local import_target = nil
for _, candidate in ipairs(candidates) do
  local first_file = r.EnumerateFiles(candidate, 0)
  if first_file and first_file ~= "" then
    import_target = candidate
    break
  end
end

if not import_target then
  fail(TITLE, "Stem split produced no output files in " .. out_dir)
  return
end

local importer = r.GetResourcePath() .. "/Scripts/Suavecito/Suavecito_SoundBoost_Stem_Import.lua"
if not file_exists(importer) then
  fail(TITLE, "Missing stem importer script: " .. importer)
  return
end

-- Route separated stems into Suavecito_SoundBoost_Stem_Import.lua (folder buses + Master 18-Band EQ)
r.SetExtState("Suavecito", "ImportStemsFolder", import_target, false)
r.SetExtState("Suavecito", "ImportStemsDrumTrigger", "1", false)
dofile(importer)
