-- Actas · Hyprland rules. Install as ~/.config/hypr/actas.lua and add
--   require("actas")
-- to hyprland.lua.

-- The recording panel is a layer surface (namespace "actas-popover"),
-- anchored top-right below the bar. No slide animation.
hl.layer_rule({
    name    = "actas-popover-layer",
    match   = { namespace = "^actas-popover$" },
    no_anim = true,
})

-- Fallback when layer-shell is unavailable: a pinned floating window.
hl.window_rule({
    name  = "actas-popover-window",
    match = { class = "^actas-popover$" },
    float = true,
    pin   = true,
    move  = "monitor_w-window_w-16 56",
})
