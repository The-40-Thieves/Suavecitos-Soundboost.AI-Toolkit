-- @description Suavecito's SoundBoost.AI Multitrack Stem, 5-Piece Drum Kit & Bus Importer
-- @author Suavecito (The 40 Thieves)
-- @version 2.0.0
-- @about
--   Imports a folder of separated SoundBoost Studio stems (Vocals, Voice, Artifacts,
--   Bass, Drums, Guitar, Piano, Other + 5-Piece Drum Kit sub-stems: Kick, Snare,
--   Toms, Hi-Hat, Cymbals) into hierarchical REAPER 7.82 folder buses (DRUM KIT BUS,
--   VOCAL BUS, INSTRUMENT BUS) with native track icons, optional JS: midi/drumtrigger
--   MIDI note extraction on 5-piece drum stems, and inserts the Suavecito SoundBoost
--   18-Band Mastering EQ (gmem=SuavecitoSoundBoost) + Loudness Meter on the Master track.

local r = reaper

local STEM_SPECS = {
  { match = "kick",      label = "Kick",      group = "drums",  icon = "kick.png",            midi_note = 36, color = {14, 165, 233} },
  { match = "snare",     label = "Snare",     group = "drums",  icon = "snare_top.png",       midi_note = 38, color = {59, 130, 246} },
  { match = "tom",       label = "Toms",      group = "drums",  icon = "tom.png",             midi_note = 45, color = {99, 102, 241} },
  { match = "hi-hat",    label = "Hi-Hat",    group = "drums",  icon = "hihat.png",           midi_note = 42, color = {139, 92, 246} },
  { match = "hihat",     label = "Hi-Hat",    group = "drums",  icon = "hihat.png",           midi_note = 42, color = {139, 92, 246} },
  { match = "cymbal",    label = "Cymbals",   group = "drums",  icon = "overheads.png",       midi_note = 49, color = {168, 85, 247} },
  { match = "drum",      label = "Drums",     group = "drums",  icon = "drum.png",            midi_note = nil, color = {56, 189, 248} },
  { match = "vocal",     label = "Vocals",    group = "vocals", icon = "mic_condenser_1.png", midi_note = nil, color = {255, 92, 138} },
  { match = "voice",     label = "Voice",     group = "vocals", icon = "mic_dynamic_1.png",   midi_note = nil, color = {244, 114, 182} },
  { match = "artifact",  label = "Artifacts", group = "vocals", icon = "fx.png",              midi_note = nil, color = {190, 24, 93} },
  { match = "bass",      label = "Bass",      group = "inst",   icon = "synthbass.png",       midi_note = nil, color = {255, 170, 45} },
  { match = "guitar",    label = "Guitar",    group = "inst",   icon = "guitar_full.png",     midi_note = nil, color = {52, 211, 153} },
  { match = "piano",     label = "Piano",     group = "inst",   icon = "piano.png",           midi_note = nil, color = {163, 230, 53} },
  { match = "other",     label = "Other",     group = "inst",   icon = "synth.png",           midi_note = nil, color = {148, 163, 184} },
}

local BUS_SPECS = {
  drums  = { name = "DRUM KIT BUS",   icon = "folder_drums.png",  color = {14, 116, 144} },
  vocals = { name = "VOCAL BUS",      icon = "folder_vocals.png", color = {190, 24, 93} },
  inst   = { name = "INSTRUMENT BUS", icon = "folder_synth.png",  color = {21, 128, 61} },
}

local function classify_stem(filename)
  local lower = filename:lower()
  for _, spec in ipairs(STEM_SPECS) do
    if lower:find(spec.match, 1, true) then
      return spec
    end
  end
  return { match = "other", label = filename, group = "inst", icon = "WAVE.png", midi_note = nil, color = {148, 163, 184} }
end

local function create_configured_track(idx, name, rgb, icon_name, folder_depth)
  r.InsertTrackAtIndex(idx, true)
  local tr = r.GetTrack(0, idx)
  if tr then
    r.GetSetMediaTrackInfo_String(tr, "P_NAME", name, true)
    local native_col = r.ColorToNative(rgb[1], rgb[2], rgb[3]) | 0x1000000
    r.SetTrackColor(tr, native_col)
    if icon_name and icon_name ~= "" then
      r.GetSetMediaTrackInfo_String(tr, "P_ICON", icon_name, true)
    end
    r.SetMediaTrackInfo_Value(tr, "I_FOLDERDEPTH", folder_depth or 0)
  end
  return tr
end

local function insert_media_on_track(tr, file_path)
  r.SetOnlyTrackSelected(tr)
  r.SetEditCurPos(0.0, false, false)
  r.InsertMedia(file_path, 0) -- 0 = add to current track
end

local function import_stems_from_folder(folder, add_drum_triggers)
  local buckets = { drums = {}, vocals = {}, inst = {} }
  local idx = 0
  local file = r.EnumerateFiles(folder, idx)
  while file do
    if file:match("%.wav$") or file:match("%.flac$") or file:match("%.mp3$") or file:match("%.aiff$") or file:match("%.ogg$") then
      local spec = classify_stem(file)
      table.insert(buckets[spec.group], {
        path = folder .. "/" .. file,
        file = file,
        spec = spec,
      })
    end
    idx = idx + 1
    file = r.EnumerateFiles(folder, idx)
  end

  local group_order = { "drums", "vocals", "inst" }
  for _, grp in ipairs(group_order) do
    local items = buckets[grp]
    if #items > 0 then
      local bus_meta = BUS_SPECS[grp]
      local bus_idx = r.CountTracks(0)
      create_configured_track(bus_idx, bus_meta.name, bus_meta.color, bus_meta.icon, 1)

      for i, entry in ipairs(items) do
        local is_last = (i == #items)
        local depth = is_last and -1 or 0
        local tr_idx = r.CountTracks(0)
        local tr = create_configured_track(
          tr_idx,
          entry.spec.label .. " (" .. entry.file .. ")",
          entry.spec.color,
          entry.spec.icon,
          depth
        )
        if tr then
          insert_media_on_track(tr, entry.path)
          -- Attach JS: midi/drumtrigger for 5-Piece Drum Kit sub-stems if requested
          if add_drum_triggers and entry.spec.midi_note then
            local fx_idx = r.TrackFX_AddByName(tr, "JS: midi/drumtrigger", false, -1)
            if fx_idx >= 0 then
              -- slider4 = MIDI channel (10), slider5 = MIDI note number
              r.TrackFX_SetParam(tr, fx_idx, 3, 10)
              r.TrackFX_SetParam(tr, fx_idx, 4, entry.spec.midi_note)
            end
          end
        end
      end
    end
  end

  -- Ensure Suavecito 18-Band Mastering EQ + Loudness Meter are on Master Track
  local master_tr = r.GetMasterTrack(0)
  if master_tr then
    r.TrackFX_AddByName(master_tr, "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx", false, 1)
    r.TrackFX_AddByName(master_tr, "JS: analysis/loudness_meter", false, 1)
  end
end

local function main()
  local ext_folder = r.GetExtState("Suavecito", "ImportStemsFolder")
  local ext_trig = r.GetExtState("Suavecito", "ImportStemsDrumTrigger")
  local folder = ""
  local add_triggers = true

  if ext_folder and ext_folder ~= "" then
    folder = ext_folder
    add_triggers = (ext_trig ~= "0")
    r.SetExtState("Suavecito", "ImportStemsFolder", "", false)
  else
    local retval, user_str = r.GetUserInputs(
      "Suavecito SoundBoost Stem & 5-Piece Drum Importer",
      2,
      "Stems Folder Path:,Add 5-Piece Drum MIDI Triggers (1/0):,extrawidth=260",
      ",1"
    )
    if not retval or user_str == "" then return end
    folder, ext_trig = user_str:match("^(.*),([^,]*)$")
    if not folder or folder == "" then return end
    add_triggers = (ext_trig ~= "0")
  end

  r.Undo_BeginBlock()
  r.PreventUIRefresh(1)
  import_stems_from_folder(folder, add_triggers)
  r.PreventUIRefresh(-1)
  r.TrackList_AdjustWindows(false)
  r.UpdateArrange()
  r.Undo_EndBlock("Import SoundBoost Stems, Folder Buses & Master EQ", -1)
end

main()
