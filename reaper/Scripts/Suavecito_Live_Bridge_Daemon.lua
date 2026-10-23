-- @description Suavecito's SoundBoost.AI Live REAPER 7.82 Bridge Daemon (gmem + File Mailbox + ExtState)
-- @author Suavecito (The 40 Thieves)
-- @version 2.0.0
-- @about
--   Background ReaScript daemon (`reaper.defer()`) that attaches to shared memory
--   `gmem_attach("SuavecitoSoundBoost")`, syncs 18-band Mastering EQ & loudness
--   telemetry in real time, and processes cross-desktop JSON RPC commands from
--   `%APPDATA%/REAPER/Suavecito/ipc_inbox.json` -> `ipc_outbox.json`.

local r = reaper

r.gmem_attach("SuavecitoSoundBoost")

local resource_path = r.GetResourcePath()
local ipc_dir = resource_path .. "/Suavecito"
r.RecursiveCreateDirectory(ipc_dir, 0)

local inbox_path = ipc_dir .. "/ipc_inbox.json"
local outbox_path = ipc_dir .. "/ipc_outbox.json"
local telemetry_path = ipc_dir .. "/telemetry.json"

local last_seq_id = ""
local tick_counter = 0

local function json_escape(s)
  if s == nil then return "" end
  s = tostring(s)
  s = s:gsub('\\', '\\\\')
  s = s:gsub('"', '\\"')
  s = s:gsub('\n', '\\n')
  s = s:gsub('\r', '\\r')
  s = s:gsub('\t', '\\t')
  return s
end

local function write_file_atomic(path, content)
  local tmp = path .. ".tmp"
  local f = io.open(tmp, "wb")
  if not f then return false end
  f:write(content)
  f:close()
  os.remove(path)
  os.rename(tmp, path)
  return true
end

local function read_file_text(path)
  local f = io.open(path, "rb")
  if not f then return nil end
  local text = f:read("*a")
  f:close()
  return text
end

local function collect_telemetry_json()
  local play_state = r.GetPlayState()
  local play_pos = (play_state & 1) == 1 and r.GetPlayPosition() or r.GetCursorPosition()
  local bpm, bpi = r.GetProjectTimeSignature2(0)
  local track_count = r.CountTracks(0)
  local _, proj_name = r.EnumProjects(-1, "")

  -- Read gmem telemetry from SoundBoost_18Band_Mastering_EQ_Monitor.jsfx
  local gmem_hb = r.gmem_read(0)
  local gmem_srate = r.gmem_read(1)
  local gmem_peak_db = r.gmem_read(3)
  local gmem_gr_db = r.gmem_read(4)
  local gmem_corr = r.gmem_read(5)
  local gmem_width = r.gmem_read(6)
  local gmem_drive = r.gmem_read(7)
  local gmem_ceil = r.gmem_read(8)

  local bands_str = {}
  for i = 0, 17 do
    table.insert(bands_str, string.format("%.2f", r.gmem_read(10 + i)))
  end

  -- Check Master track FX & Cockos loudness_meter if loaded
  local master_tr = r.GetMasterTrack(0)
  local master_fx = {}
  local lufs_i, lufs_s, lufs_m, true_peak_dbtp, lra = 0.0, 0.0, 0.0, 0.0, 0.0
  if master_tr then
    local fx_cnt = r.TrackFX_GetCount(master_tr)
    for fx = 0, fx_cnt - 1 do
      local _, fx_name = r.TrackFX_GetFXName(master_tr, fx, "")
      table.insert(master_fx, '"' .. json_escape(fx_name) .. '"')
      if fx_name:lower():find("loudness_meter", 1, true) then
        -- Cockos JS: analysis/loudness_meter output sliders (0-based indices 29..35)
        true_peak_dbtp = r.TrackFX_GetParam(master_tr, fx, 30)
        lufs_m = r.TrackFX_GetParam(master_tr, fx, 31)
        lufs_s = r.TrackFX_GetParam(master_tr, fx, 32)
        lufs_i = r.TrackFX_GetParam(master_tr, fx, 33)
        lra = r.TrackFX_GetParam(master_tr, fx, 34)
      end
    end
  end

  local tracks_json = {}
  for t = 0, math.min(track_count - 1, 63) do
    local tr = r.GetTrack(0, t)
    if tr then
      local _, t_name = r.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
      local _, t_icon = r.GetSetMediaTrackInfo_String(tr, "P_ICON", "", false)
      local depth = math.floor(r.GetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH"))
      local fx_cnt = r.TrackFX_GetCount(tr)
      table.insert(
        tracks_json,
        string.format(
          '{"index":%d,"name":"%s","icon":"%s","folder_depth":%d,"fx_count":%d}',
          t,
          json_escape(t_name),
          json_escape(t_icon),
          depth,
          fx_cnt
        )
      )
    end
  end

  return string.format(
    '{"status":"ok","reaper_version":"%s","project_path":"%s","play_state":%d,"play_position_sec":%.3f,"bpm":%.2f,"bpi":%.0f,"track_count":%d,' ..
    '"gmem":{"heartbeat":%.0f,"sample_rate":%.0f,"peak_hold_db":%.2f,"gr_meter_db":%.2f,"stereo_corr":%.3f,"stereo_width_pct":%.1f,"makeup_drive_db":%.2f,"ceiling_dbtp":%.2f,"eq_18band_db":[%s]},' ..
    '"master_loudness":{"lufs_i":%.2f,"lufs_s":%.2f,"lufs_m":%.2f,"true_peak_dbtp":%.2f,"lra_lu":%.2f},' ..
    '"master_fx":[%s],"tracks":[%s]}',
    json_escape(r.GetAppVersion()),
    json_escape(proj_name),
    play_state,
    play_pos,
    bpm,
    bpi,
    track_count,
    gmem_hb,
    gmem_srate,
    gmem_peak_db,
    gmem_gr_db,
    gmem_corr,
    gmem_width,
    gmem_drive,
    gmem_ceil,
    table.concat(bands_str, ","),
    lufs_i,
    lufs_s,
    lufs_m,
    true_peak_dbtp,
    lra,
    table.concat(master_fx, ","),
    table.concat(tracks_json, ",")
  )
end

-- Simple key extractors for flat/structured JSON RPC inbox
local function extract_json_string(raw, key)
  local pat = '"' .. key .. '"%s*:%s*"([^"]*)"'
  return raw:match(pat)
end

local function extract_json_number(raw, key)
  local pat = '"' .. key .. '"%s*:%s*([%-]?%d+%.?%d*)'
  local v = raw:match(pat)
  return v and tonumber(v) or nil
end

local function extract_json_num_array(raw, key)
  local pat = '"' .. key .. '"%s*:%s*%[([^%]]*)%]'
  local body = raw:match(pat)
  if not body then return nil end
  local arr = {}
  for num in body:gmatch("([%-]?%d+%.?%d*)") do
    table.insert(arr, tonumber(num))
  end
  return arr
end

local function handle_inbox_command(raw)
  local req_id = extract_json_string(raw, "request_id") or tostring(os.time())
  if req_id == last_seq_id then return end
  last_seq_id = req_id

  local cmd = extract_json_string(raw, "command") or "telemetry"
  local detail_msg = "executed"

  if cmd == "set_eq_18band" then
    local master_tr = r.GetMasterTrack(0)
    if master_tr then
      r.TrackFX_AddByName(master_tr, "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx", false, 1)
    end
    local gains = extract_json_num_array(raw, "eq_18band_db")
    if gains then
      for i = 1, 18 do
        r.gmem_write(99 + i, gains[i] or 0.0)
      end
    end
    local width = extract_json_number(raw, "stereo_width_pct") or 100.0
    local drive = extract_json_number(raw, "makeup_drive_db") or 0.0
    local ceil = extract_json_number(raw, "ceiling_dbtp") or -1.0
    r.gmem_write(118, width)
    r.gmem_write(119, drive)
    r.gmem_write(120, ceil)
    local next_seq = (r.gmem_read(127) or 0) + 1
    r.gmem_write(127, next_seq)
    detail_msg = "gmem_eq_updated_seq_" .. tostring(math.floor(next_seq))

  elseif cmd == "import_stems" then
    local folder = extract_json_string(raw, "stems_folder") or ""
    local trig = extract_json_number(raw, "add_drum_triggers")
    if folder ~= "" then
      r.SetExtState("Suavecito", "ImportStemsFolder", folder, false)
      r.SetExtState("Suavecito", "ImportStemsDrumTrigger", (trig == 0) and "0" or "1", false)
      local script_file = resource_path .. "/Scripts/Suavecito/Suavecito_SoundBoost_Stem_Import.lua"
      local f = io.open(script_file, "r")
      if f then
        f:close()
        dofile(script_file)
        detail_msg = "imported_stems_from_" .. folder
      else
        detail_msg = "stem_importer_script_missing"
      end
    end

  elseif cmd == "set_render_metadata" then
    local meta = extract_json_string(raw, "render_metadata") or ""
    if meta ~= "" then
      r.GetSetProjectInfo_String(0, "RENDER_METADATA", meta, true)
      detail_msg = "render_metadata_applied"
    end

  elseif cmd == "add_marker_or_region" then
    local pos = extract_json_number(raw, "pos_sec") or 0.0
    local rgn_end = extract_json_number(raw, "end_sec") or 0.0
    local is_rgn = (extract_json_number(raw, "is_region") or 0) == 1
    local label = extract_json_string(raw, "label") or "SoundBoost Marker"
    local r_col = extract_json_number(raw, "r") or 255
    local g_col = extract_json_number(raw, "g") or 92
    local b_col = extract_json_number(raw, "b") or 138
    local color = r.ColorToNative(r_col, g_col, b_col) | 0x1000000
    r.AddProjectMarker2(0, is_rgn, pos, rgn_end, label, -1, color)
    r.UpdateArrange()
    detail_msg = "marker_added_" .. label

  elseif cmd == "calculate_file_loudness" then
    local file_path = extract_json_string(raw, "file_path") or ""
    if file_path ~= "" then
      local src = r.PCM_Source_CreateFromFile(file_path)
      if src then
        -- normalizeTo=0 (LUFS-I), target=-14.0 -> returns gain needed in dB to hit -14 LUFS-I
        local gain_to_m14 = r.CalculateNormalization(src, 0, -14.0, 0.0, 0.0)
        local gain_to_0tp = r.CalculateNormalization(src, 3, 0.0, 0.0, 0.0)
        r.PCM_Source_Destroy(src)
        local calc_lufs_i = -14.0 - gain_to_m14
        local calc_tp = -gain_to_0tp
        detail_msg = string.format("native_lufs_i=%.2f,native_true_peak_dbtp=%.2f", calc_lufs_i, calc_tp)
      end
    end

  elseif cmd == "insert_video_item" then
    local video_path = extract_json_string(raw, "video_path") or ""
    local pos_sec = extract_json_number(raw, "pos_sec") or 0.0
    local open_vwnd = extract_json_number(raw, "open_video_window") or 0
    if video_path ~= "" then
      local v_tr = nil
      local cnt = r.CountTracks(0)
      for i = 0, cnt - 1 do
        local tr = r.GetTrack(0, i)
        local _, tname = r.GetSetMediaTrackInfo_String(tr, "P_NAME", "", false)
        if tname:upper():find("VIDEO", 1, true) or tname:upper():find("SHOOTS", 1, true) then
          v_tr = tr
          break
        end
      end
      if not v_tr then
        r.InsertTrackAtIndex(cnt, true)
        v_tr = r.GetTrack(0, cnt)
        if v_tr then
          r.GetSetMediaTrackInfo_String(v_tr, "P_NAME", "SHOOTS / 9:16 SPOTIFY CANVAS VIDEO (h3-max gmem)", true)
          r.GetSetMediaTrackInfo_String(v_tr, "P_ICON", "video.png", true)
          r.SetTrackColor(v_tr, r.ColorToNative(236, 72, 153) | 0x1000000)
          r.TrackFX_AddByName(v_tr, "Video processor", false, 1)
        end
      end
      if v_tr then
        r.SetOnlyTrackSelected(v_tr)
        r.SetEditCurPos(pos_sec, false, false)
        r.InsertMedia(video_path, 0)
        local master_tr = r.GetMasterTrack(0)
        if master_tr then
          r.TrackFX_AddByName(master_tr, "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx", false, 1)
          r.TrackFX_AddByName(master_tr, "JS: misc/video_sample_peeker", false, 1)
        end
        if open_vwnd == 1 then
          r.Main_OnCommand(50125, 0) -- View: Toggle video window
        end
        r.UpdateArrange()
        detail_msg = "inserted_video_item_" .. video_path
      end
    end

  elseif cmd == "run_action" then
    local action_id = extract_json_string(raw, "action_id") or ""
    if action_id ~= "" then
      local num_id = tonumber(action_id)
      if not num_id then
        num_id = r.NamedCommandLookup(action_id)
      end
      if num_id and num_id > 0 then
        r.Main_OnCommand(num_id, 0)
        detail_msg = "ran_action_" .. tostring(num_id)
      end
    end
  end

  local telem = collect_telemetry_json()
  local out_payload = string.format(
    '{"request_id":"%s","command":"%s","result":"%s","telemetry":%s}',
    json_escape(req_id),
    json_escape(cmd),
    json_escape(detail_msg),
    telem
  )
  write_file_atomic(outbox_path, out_payload)
  r.SetExtState("Suavecito", "LastBridgeResponse", out_payload, false)
end

local function bridge_loop()
  tick_counter = tick_counter + 1

  -- Poll inbox every ~2 ticks (~60ms)
  if (tick_counter % 2) == 0 then
    local raw = read_file_text(inbox_path)
    if raw and #raw > 2 then
      handle_inbox_command(raw)
    end
  end

  -- Refresh telemetry snapshot every ~15 ticks (~0.5s)
  if (tick_counter % 15) == 0 then
    local telem = collect_telemetry_json()
    write_file_atomic(telemetry_path, telem)
    r.SetExtState("Suavecito", "LiveTelemetry", telem, false)
  end

  r.defer(bridge_loop)
end

r.SetExtState("Suavecito", "BridgeDaemonStatus", "running", false)
bridge_loop()
