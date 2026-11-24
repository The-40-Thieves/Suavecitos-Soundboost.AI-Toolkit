-- @description Suavecito Auto Vocal Chain (Denoise + Pedalboard + VST3 Rack + -14 LUFS)
-- @version 1.1.0
-- @author Suavecito SoundBoost + Antigravity

local r = reaper
local TITLE = "Suavecito Auto Vocal Chain"
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
  fail(TITLE, "Please select a raw vocal audio item first.")
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

local lufs_str = r.GetExtState("Suavecito", "VocalChainLufs")
local lufs = (lufs_str ~= "") and tonumber(lufs_str) or -14.0
if not lufs or lufs < -36.0 or lufs > -1.0 then
  fail(TITLE, "VocalChainLufs must be between -36.0 and -1.0 LUFS.")
  return
end

local out_path = with_suffix(src_path, "_polished_24bit.wav")
local bin = resolve_tool_bin("VocalMusicKitBin", "SUAVECITO_VOCAL_MUSIC_KIT", "vocal-music-kit")
local cmd, cmd_err = build_command(bin, {
  "vocal-chain",
  src_path,
  out_path,
  "--use-vst3",
  "--lufs",
  string.format("%.1f", lufs),
})
if not cmd then
  fail(TITLE, cmd_err)
  return
end

r.ShowConsoleMsg("[Suavecito Auto Vocal Chain] Processing vocal:\n" .. cmd .. "\n")
local ok, exit_code, output = run_exec(cmd)
if not ok then
  fail(TITLE, string.format("Auto vocal chain failed (exit %d): %s", exit_code, output))
  return
end

if not file_exists(out_path) then
  fail(TITLE, "Expected polished vocal output file was not created: " .. out_path)
  return
end

r.Undo_BeginBlock()
r.InsertMedia(out_path, 3) -- Add as new active take on selected item
r.UpdateArrange()
r.Undo_EndBlock("Suavecito Auto Vocal Chain", -1)
r.ShowConsoleMsg("[Suavecito Auto Vocal Chain] Added polished 24-bit take -> " .. out_path .. "\n")
