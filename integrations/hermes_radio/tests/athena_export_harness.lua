-- Test harness for infra/dcs-client/AthenaExport.lua (run with lua5.1, DCS uses Lua 5.1).
-- Stubs the DCS Export API and LuaSocket, loads the script twice (idempotence), drives the
-- Export callbacks and prints every UDP packet as one line: "PKT <json>". Also prints chain
-- markers so the test can check that the previous Export callbacks still run.
-- usage: lua5.1 athena_export_harness.lua <path to AthenaExport.lua> [nil-mode]
local path, mode = arg[1], arg[2]

local clock = 1000.0
local sent = {}
local udp = {}
function udp:settimeout() end
function udp:setpeername(host, port) print("PEER " .. host .. ":" .. port) end
function udp:send(msg) sent[#sent + 1] = msg; return #msg end
function udp:close() end
package.loaded.socket = { udp = function() return udp end, gettime = function() return clock end }
log = { INFO = 1, write = function(_, _, msg) print("LOG " .. msg) end }

-- Previous Export callbacks (SRS / Tacview style) that must keep running.
function LuaExportStart() print("PREV start") end
function LuaExportStop() print("PREV stop") end
function LuaExportAfterNextFrame() print("PREV after") end
function LuaExportActivityNextEvent(t) return t + 1 end
local activity = LuaExportActivityNextEvent

local rad = math.pi / 180
if mode ~= "nil" then
    function LoGetModelTime() return 123.456 end
    function LoGetSelfData()
        return { Name = "F-16C_50", UnitName = "Viper 1-1", GroupName = "Viper 1", CoalitionID = 2,
                 LatLongAlt = { Lat = 36.2364567, Long = -115.0345678, Alt = 3048.0 },
                 Heading = 90 * rad, Pitch = 2 * rad, Bank = -10 * rad }
    end
    function LoGetPilotName() return "Prometheus" end
    function LoGetMagneticYaw() return 78 * rad end
    function LoGetIndicatedAirSpeed() return 180.0 end
    function LoGetTrueAirSpeed() return 200.0 end
    function LoGetVectorVelocity() return { x = 3, y = 1, z = 4 } end
    function LoGetMachNumber() return 0.62 end
    function LoGetAltitudeAboveSeaLevel() return 3048.0 end
    function LoGetAltitudeAboveGroundLevel() return 2100.0 end
    function LoGetVerticalVelocity() return -5.08 end
    function LoGetAngleOfAttack() return 5 * rad end
    function LoGetAccelerationUnits() return { x = 0.1, y = 4.5, z = 0 } end
    function LoGetEngineInfo() return { fuel_internal = 2500, fuel_external = 1200, RPM = { left = 88.5, right = 0 } } end
    function LoGetNameByType(a, b, c, d)
        local names = { ["4.4.7.24"] = "AIM-120C", ["4.4.7.7"] = "AIM-9X", ["4.5.36.38"] = "GBU-12",
                        ["2.16.103.115"] = "SA-11 Buk SR 9S18M1", ["1.1.1.3"] = "Su-27" }
        return names[table.concat({ a, b, c, d }, ".")]
    end
    function LoGetTWSInfo()
        return { Mode = 0, Emitters = {
            { ID = 16777300, Type = { level1 = 2, level2 = 16, level3 = 103, level4 = 115 }, Power = 0.71,
              Azimuth = 90 * rad, Priority = 200, SignalType = "lock" },
            { ID = 16777400, Type = { level1 = 1, level2 = 1, level3 = 1, level4 = 3 }, Power = 0.2,
              Azimuth = -45 * rad, Priority = 100, SignalType = "scan" } } }
    end
    function LoGetLockedTargetInformation()
        return { { ID = 16777400, type = { level1 = 1, level2 = 1, level3 = 1, level4 = 3 }, distance = 37040,
                   convergence_velocity = 250, mach = 0.9, fim = 10 * rad, fin = 1 * rad, flags = 3,
                   position = { p = { x = 1000, y = 6000, z = 2000 } } } }
    end
    function LoGetTargetInformation() return LoGetLockedTargetInformation() end
    function LoLoCoordinatesToGeoCoordinates(x, z) return { latitude = 36.5, longitude = -114.9 } end
    function LoGetPayloadInfo()
        return { CurrentStation = 3, Cannon = { shells = 510 }, Stations = {
            { CLSID = "a", count = 1, weapon = { level1 = 4, level2 = 4, level3 = 7, level4 = 24 } },
            { CLSID = "b", count = 1, weapon = { level1 = 4, level2 = 4, level3 = 7, level4 = 24 } },
            { CLSID = "c", count = 2, weapon = { level1 = 4, level2 = 5, level3 = 36, level4 = 38 } },
            { CLSID = "d", count = 0, weapon = { level1 = 4, level2 = 4, level3 = 7, level4 = 7 } },
            { CLSID = "tank", count = 1, weapon = { level1 = 1, level2 = 3, level3 = 43, level4 = 11 } } } }
    end
    function LoGetMechInfo()
        return { gear = { status = 1, value = 1 }, flaps = { status = 1, value = 0.5 },
                 speedbrakes = { status = 0, value = 0 }, hook = { status = 0, value = 0 } }
    end
    function LoGetMCPState() return { MasterWarning = true, LeftEngineFailure = false, FuelLow = true } end
else
    -- MP server with exports disabled / aircraft without data: everything nil, some functions error.
    function LoGetSelfData() return nil end
    function LoGetTWSInfo() error("not available") end
end

dofile(path)
dofile(path) -- loader line run twice: must not double-chain
print("ACTIVITY " .. tostring(LuaExportActivityNextEvent == activity))

LuaExportStart()
for i = 1, 12 do
    clock = clock + 0.05 -- 20 fps: about every 4th frame sends (5 Hz)
    LuaExportAfterNextFrame()
end
LuaExportStop()
for _, msg in ipairs(sent) do print("PKT " .. msg) end
print("ERRORS " .. tostring(AthenaExport.errors))
