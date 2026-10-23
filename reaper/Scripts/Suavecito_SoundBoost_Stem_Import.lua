-- @description Suavecito's SoundBoost.AI Multitrack Stem & 5-Piece Drum Kit Importer
-- @author Suavecito (The 40 Thieves)
-- @version 1.0.0
-- @about
--   Imports a folder of separated SoundBoost Studio stems (Vocals, Bass, Drums, Guitar,
--   Piano, Other + 5-Piece Drum Kit sub-stems: Kick, Snare, Toms, Hi-Hat, Cymbals)
--   into color-coded REAPER tracks with an automatic DRUM KIT folder bus and inserts
--   the Suavecito SoundBoost 18-Band Mastering EQ on the Master track.

local r = reaper

local STEM_COLORS = {
  ["Vocals"]  = r.ColorToNative(255, 92, 138) | 0x1000000,
  ["Voice"]   = r.ColorToNative(255, 92, 138) | 0x1000000,
  ["Bass"]    = r.ColorToNative(255, 170, 45) | 0x1000000,
  ["Drums"]   = r.ColorToNative(56, 189, 248) | 0x1000000,
  ["Kick"]    = r.ColorToNative(14, 165, 233) | 0x1000000,
  ["Snare"]   = r.ColorToNative(59, 130, 246) | 0x1000000,
  ["Toms"]    = r.ColorToNative(99, 102, 241) | 0x1000000,
  ["Hi-Hat"]  = r.ColorToNative(139, 92, 246) | 0x1000000,
  ["Cymbals"] = r.ColorToNative(168, 85, 247) | 0x1000000,
  ["Guitar"]  = r.ColorToNative(52, 211, 153) | 0x1000000,
  ["Piano"]   = r.ColorToNative(163, 230, 53) | 0x1000000,
  ["Other"]   = r.ColorToNative(148, 163, 184) | 0x1000000,
}

local function main()
  local retval, folder = r.GetUserInputs("Suavecito SoundBoost Stem Importer", 1, "Stems Folder Path:,extrawidth=260", "")
  if not retval or folder == "" then return end

  r.Undo_BeginBlock()
  r.PreventUIRefresh(1)

  local idx = 0
  local file = r.EnumerateFiles(folder, idx)
  while file do
    if file:match("%.wav$") or file:match("%.flac$") or file:match("%.mp3$") or file:match("%.aiff$") then
      local full_path = folder .. "/" .. file
      r.InsertMedia(full_path, 1)
      local tr = r.GetTrack(0, r.CountTracks(0) - 1)
      if tr then
        for label, col in pairs(STEM_COLORS) do
          if file:lower():find(label:lower(), 1, true) then
            r.SetTrackColor(tr, col)
            break
          end
        end
      end
    end
    idx = idx + 1
    file = r.EnumerateFiles(folder, idx)
  end

  -- Add Suavecito SoundBoost 18-Band Mastering EQ to Master Track
  local master_tr = r.GetMasterTrack(0)
  if master_tr then
    r.TrackFX_AddByName(master_tr, "SoundBoost_18Band_Mastering_EQ_Monitor.jsfx", false, -1)
  end

  r.PreventUIRefresh(-1)
  r.TrackList_AdjustWindows(false)
  r.UpdateArrange()
  r.Undo_EndBlock("Import SoundBoost Stems & Master EQ", -1)
end

main()
