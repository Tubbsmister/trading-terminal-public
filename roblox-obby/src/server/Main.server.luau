--!strict
-- Server entry: builds the course, tracks stages, saves progress, and handles
-- game passes and developer products.

local Players = game:GetService("Players")
local MarketplaceService = game:GetService("MarketplaceService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")

local Config = require(ReplicatedStorage.Shared.Config)
local Obby = require(script.Parent.Obby)
local Data = require(script.Parent.Data)
local Perks = require(script.Parent.Perks)

Players.CharacterAutoLoads = false
Players.RespawnTime = 1.5

local remotes = Instance.new("Folder")
remotes.Name = "Remotes"
local function remote(name: string): RemoteEvent
	local r = Instance.new("RemoteEvent")
	r.Name = name
	r.Parent = remotes
	return r
end
local Notify = remote("Notify") -- (text: string, kind: string)
local SkipNudge = remote("SkipNudge") -- (stage: number)
local Restart = remote("Restart")
remotes.Parent = ReplicatedStorage

local course = Obby.build()

type Session = {
	profile: Data.Profile,
	stageValue: IntValue,
	winsValue: IntValue,
	deathsOnStage: number,
	dirty: boolean,
}
local sessions: { [Player]: Session } = {}

local function checkpointCFrame(stage: number): CFrame
	local pad = course.Checkpoints[math.clamp(stage, 1, #course.Checkpoints)]
	return pad.CFrame * CFrame.new(0, 4, 0)
end

local function teleportToCheckpoint(player: Player)
	local s = sessions[player]
	local char = player.Character
	if s and char and char.PrimaryPart then
		char:PivotTo(checkpointCFrame(s.profile.Stage))
	end
end

local function setStage(player: Player, stage: number)
	local s = sessions[player]
	if not s then
		return
	end
	stage = math.clamp(stage, 1, Config.StageCount)
	if stage == s.profile.Stage then
		return
	end
	s.profile.Stage = stage
	s.stageValue.Value = stage
	s.deathsOnStage = 0
	s.dirty = true
	player:SetAttribute("Stage", stage)
end

local function spawn(player: Player)
	if not player.Parent or not sessions[player] then
		return
	end
	if pcall(player.LoadCharacter, player) then
		teleportToCheckpoint(player)
	end
end

local function onCharacter(player: Player, char: Model)
	local hum = char:WaitForChild("Humanoid") :: Humanoid
	Perks.applyToCharacter(player, char)
	hum.Died:Connect(function()
		local s = sessions[player]
		if s then
			s.deathsOnStage += 1
			if s.deathsOnStage == Config.DeathsBeforeSkipNudge and s.profile.Stage < Config.StageCount then
				SkipNudge:FireClient(player, s.profile.Stage)
			end
		end
		task.wait(Players.RespawnTime)
		if player.Character == char then
			spawn(player)
		end
	end)
end

local function onPlayerAdded(player: Player)
	local profile = Data.load(player.UserId)
	if not player.Parent then
		return
	end

	local leaderstats = Instance.new("Folder")
	leaderstats.Name = "leaderstats"
	local stageValue = Instance.new("IntValue")
	stageValue.Name = "Stage"
	stageValue.Value = profile.Stage
	stageValue.Parent = leaderstats
	local winsValue = Instance.new("IntValue")
	winsValue.Name = "Wins"
	winsValue.Value = profile.Wins
	winsValue.Parent = leaderstats
	leaderstats.Parent = player

	sessions[player] = {
		profile = profile,
		stageValue = stageValue,
		winsValue = winsValue,
		deathsOnStage = 0,
		dirty = false,
	}
	player:SetAttribute("Stage", profile.Stage)
	player:SetAttribute("StageCount", Config.StageCount)

	Perks.loadOwnership(player)
	player.CharacterAdded:Connect(function(char)
		onCharacter(player, char)
	end)
	spawn(player)
end

local function saveSession(player: Player)
	local s = sessions[player]
	if s then
		Data.save(player.UserId, s.profile)
		s.dirty = false
	end
end

Players.PlayerAdded:Connect(onPlayerAdded)
for _, p in Players:GetPlayers() do
	task.spawn(onPlayerAdded, p)
end

Players.PlayerRemoving:Connect(function(player)
	saveSession(player)
	sessions[player] = nil
end)

game:BindToClose(function()
	for player in sessions do
		task.spawn(saveSession, player)
	end
	task.wait(3)
end)

-- Autosave dirty sessions every minute.
task.spawn(function()
	while true do
		task.wait(60)
		for player, s in sessions do
			if s.dirty then
				task.spawn(saveSession, player)
			end
		end
	end
end)

-- Checkpoints only ever move a player forward.
for index, pad in course.Checkpoints do
	pad.Touched:Connect(function(hit)
		local player = Players:GetPlayerFromCharacter(hit.Parent)
		local s = player and sessions[player]
		if player and s and index > s.profile.Stage then
			local hum = (hit.Parent :: Instance):FindFirstChildOfClass("Humanoid")
			if hum and hum.Health > 0 then
				setStage(player, index)
				Notify:FireClient(player, `Checkpoint! Stage {index}`, "stage")
			end
		end
	end)
end

local finishDebounce: { [Player]: boolean } = {}
course.Finish.Touched:Connect(function(hit)
	local player = Players:GetPlayerFromCharacter(hit.Parent)
	local s = player and sessions[player]
	if not (player and s) or finishDebounce[player] or s.profile.Stage < Config.StageCount then
		return
	end
	finishDebounce[player] = true
	s.profile.Wins += 1
	s.winsValue.Value = s.profile.Wins
	s.dirty = true
	player:SetAttribute("Completed", true)
	Notify:FireClient(player, `YOU BEAT {string.upper(Config.GameName)}! Wins: {s.profile.Wins}`, "win")
	saveSession(player)
	task.wait(3)
	finishDebounce[player] = nil
end)

Restart.OnServerEvent:Connect(function(player)
	local s = sessions[player]
	if s and player:GetAttribute("Completed") then
		player:SetAttribute("Completed", nil)
		setStage(player, 1)
		teleportToCheckpoint(player)
	end
end)

-- Fall detection: drop far below your checkpoint and you respawn there.
-- Stops falls from a high lap landing you on an earlier stage.
task.spawn(function()
	while true do
		task.wait(0.25)
		for player, s in sessions do
			local char = player.Character
			local root = char and char:FindFirstChild("HumanoidRootPart") :: BasePart?
			local hum = char and char:FindFirstChildOfClass("Humanoid")
			if root and hum and hum.Health > 0 and not player:GetAttribute("Completed") then
				local floorY = checkpointCFrame(s.profile.Stage).Position.Y - 30
				if root.Position.Y < floorY then
					hum.Health = 0
				end
			end
		end
	end
end)

----------------------------------------------------------------------------
-- Monetization
----------------------------------------------------------------------------

local productById: { [number]: { Stages: number, Name: string } } = {}
for _, key in Config.ProductOrder do
	local p = Config.Products[key]
	if p.Id ~= 0 then
		productById[p.Id] = p
	end
end

MarketplaceService.ProcessReceipt = function(receipt)
	local player = Players:GetPlayerByUserId(receipt.PlayerId)
	local s = player and sessions[player]
	if not (player and s) then
		return Enum.ProductPurchaseDecision.NotProcessedYet
	end
	local purchaseId = tostring(receipt.PurchaseId)
	if Data.hasReceipt(s.profile, purchaseId) then
		return Enum.ProductPurchaseDecision.PurchaseGranted
	end
	local product = productById[receipt.ProductId]
	if not product then
		warn(`Unknown developer product {receipt.ProductId}`)
		return Enum.ProductPurchaseDecision.NotProcessedYet
	end

	table.insert(s.profile.Receipts, purchaseId)
	setStage(player, s.profile.Stage + product.Stages)
	teleportToCheckpoint(player)
	Notify:FireClient(player, `Skipped to Stage {s.profile.Stage}!`, "purchase")

	local saved = Data.save(player.UserId, s.profile)
	if saved or not Data.Enabled then
		s.dirty = false
		return Enum.ProductPurchaseDecision.PurchaseGranted
	end
	-- Roblox will retry; the receipt ID in memory stops a double grant.
	return Enum.ProductPurchaseDecision.NotProcessedYet
end

MarketplaceService.PromptGamePassPurchaseFinished:Connect(function(player, passId, purchased)
	if purchased then
		local key = Perks.grantPassById(player, passId)
		if key then
			Notify:FireClient(player, `Thanks! {Config.GamePasses[key].Name} unlocked.`, "purchase")
		end
	end
end)
