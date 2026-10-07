--!strict
-- Player save data (stage, wins, processed receipt IDs) in one DataStore key.
-- Receipt IDs live in the same record so purchase grants are idempotent.

local DataStoreService = game:GetService("DataStoreService")
local RunService = game:GetService("RunService")

local Data = {}

export type Profile = {
	Stage: number,
	Wins: number,
	Receipts: { string },
}

local MAX_RECEIPTS = 100

local store: DataStore? = nil
local ok = pcall(function()
	store = DataStoreService:GetDataStore("NeonObby_v1")
end)
-- In Studio without "Enable Studio Access to API Services" this fails; the
-- game still runs, it just doesn't save.
Data.Enabled = ok and store ~= nil

local function default(): Profile
	return { Stage = 1, Wins = 0, Receipts = {} }
end

local function retry<T>(fn: () -> T): (boolean, T?)
	for attempt = 1, 3 do
		local success, result = pcall(fn)
		if success then
			return true, result
		end
		warn(`DataStore attempt {attempt} failed: {result}`)
		task.wait(attempt)
	end
	return false, nil
end

function Data.load(userId: number): Profile
	if not Data.Enabled or not store then
		return default()
	end
	local s = store :: DataStore
	local success, raw = retry(function()
		return s:GetAsync(tostring(userId))
	end)
	if not success then
		if RunService:IsStudio() then
			Data.Enabled = false
		end
		return default()
	end
	local profile = default()
	if type(raw) == "table" then
		profile.Stage = tonumber(raw.Stage) or 1
		profile.Wins = tonumber(raw.Wins) or 0
		if type(raw.Receipts) == "table" then
			profile.Receipts = raw.Receipts
		end
	end
	return profile
end

function Data.save(userId: number, profile: Profile): boolean
	if not Data.Enabled or not store then
		return false
	end
	local s = store :: DataStore
	while #profile.Receipts > MAX_RECEIPTS do
		table.remove(profile.Receipts, 1)
	end
	local success = retry(function()
		return s:UpdateAsync(tostring(userId), function(old)
			-- Keep the higher stage/wins in case another server wrote last.
			local merged = {
				Stage = profile.Stage,
				Wins = profile.Wins,
				Receipts = profile.Receipts,
			}
			if type(old) == "table" and (tonumber(old.Wins) or 0) > profile.Wins then
				merged.Wins = old.Wins
			end
			return merged
		end)
	end)
	return success
end

function Data.hasReceipt(profile: Profile, purchaseId: string): boolean
	return table.find(profile.Receipts, purchaseId) ~= nil
end

return Data
