--!nonstrict
-- Bar widget for Actas. Polls `actas status --json` once per second.
-- Left click: recording panel. Right click: start/stop.

local busy = false
local status = nil

local function cfg(key)
	return noctalia.getConfig(key)
end

local function shellQuote(value)
	return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function executable()
	local value = cfg("actas_path")
	if type(value) ~= "string" or value == "" then
		return "actas"
	end
	if value:find("/", 1, true) ~= nil then
		return noctalia.expandPath(value)
	end
	return value
end

local function run(args)
	noctalia.runAsync(shellQuote(executable()) .. " " .. args, function(_result) end, 5000)
end

local STYLE = {
	recording = { glyph = "player-record", color = "error" },
	paused = { glyph = "player-pause", color = "error" },
	suggest = { glyph = "microphone", color = "tertiary" },
	idle = { glyph = "microphone", color = "on_surface_variant" },
	offline = { glyph = "microphone-off", color = "on_surface_variant" },
}

local function render()
	local state = if type(status) == "table" and type(status.state) == "string" then status.state else "offline"
	local transcribing = type(status) == "table"
		and type(status.status) == "table"
		and status.status.transcribing ~= nil
	local style = STYLE[state] or STYLE.idle
	local text = if type(status) == "table" and type(status.text) == "string" then status.text else ""
	if state == "recording" then
		text = "REC " .. text
	end

	local quiet = (state == "idle" or state == "offline") and not transcribing
	barWidget.setVisible(not (quiet and cfg("hide_when_idle") == true))
	barWidget.setGlyph(if transcribing and state == "idle" then "file-text" else style.glyph)
	barWidget.setGlyphColor(style.color)
	barWidget.setColor(style.color)
	barWidget.setText(text)
	local tooltip = if type(status) == "table" and type(status.tooltip) == "string" then status.tooltip else "Actas"
	barWidget.setTooltip({ "Actas", tooltip, "Clic: panel · Clic derecho: grabar/detener" })
end

function update()
	if busy then
		return
	end
	busy = true
	local accepted = noctalia.runAsync(shellQuote(executable()) .. " status --json", function(result)
		busy = false
		if type(result) == "table" and not result.timedOut and result.exitCode == 0 then
			status = noctalia.json.decode(result.stdout or "")
		else
			status = nil
		end
		render()
	end, 3000)
	if not accepted then
		busy = false
	end
end

function onClick()
	run("popover")
end

function onRightClick()
	run("toggle")
	update()
end

function onIpc(event, _payload)
	if event == "toggle" then
		onRightClick()
	elseif event == "panel" then
		onClick()
	end
end

pcall(function()
	noctalia.setUpdateInterval(1000)
end)
render()
update()
