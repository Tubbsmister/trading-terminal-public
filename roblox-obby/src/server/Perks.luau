--!strict
-- Game pass ownership and the perks each pass grants.
-- Ownership is mirrored to player attributes ("Pass_VIP" etc.) for the UI.

local MarketplaceService = game:GetService("MarketplaceService")

local Config = require(game:GetService("ReplicatedStorage").Shared.Config)

local Perks = {}

local function owns(player: Player, key: string): boolean
	return player:GetAttribute("Pass_" .. key) == true
end

local function baseSpeed(player: Player): number
	return owns(player, "VIP") and Config.VipWalkSpeed or Config.BaseWalkSpeed
end

local function makeCoil(name: string, color: Color3): (Tool, BasePart)
	local tool = Instance.new("Tool")
	tool.Name = name
	tool.CanBeDropped = false
	tool.ToolTip = name
	local handle = Instance.new("Part")
	handle.Name = "Handle"
	handle.Size = Vector3.new(0.6, 1.6, 0.6)
	handle.Color = color
	handle.Material = Enum.Material.Neon
	handle.CanCollide = false
	handle.Massless = true
	handle.Parent = tool
	return tool, handle
end

local function giveSpeedCoil(player: Player)
	local tool = makeCoil("Speed Coil", Color3.fromRGB(60, 140, 255))
	tool.Equipped:Connect(function()
		local hum = tool.Parent and tool.Parent:FindFirstChildOfClass("Humanoid")
		if hum then
			hum.WalkSpeed = Config.SpeedCoilWalkSpeed
		end
	end)
	tool.Unequipped:Connect(function()
		local char = player.Character
		local hum = char and char:FindFirstChildOfClass("Humanoid")
		if hum then
			hum.WalkSpeed = baseSpeed(player)
		end
	end)
	tool.Parent = player:WaitForChild("Backpack")
end

local function giveGravityCoil(player: Player)
	local tool = makeCoil("Gravity Coil", Color3.fromRGB(170, 80, 255))
	local force: VectorForce? = nil
	tool.Equipped:Connect(function()
		local char = tool.Parent :: Model
		local root = char and char:FindFirstChild("HumanoidRootPart") :: BasePart?
		local att = root and root:FindFirstChild("RootAttachment") :: Attachment?
		if not (root and att) then
			return
		end
		local f = Instance.new("VectorForce")
		f.Attachment0 = att
		f.RelativeTo = Enum.ActuatorRelativeTo.World
		f.ApplyAtCenterOfMass = true
		f.Force = Vector3.new(0, root.AssemblyMass * workspace.Gravity * Config.GravityCoilLift, 0)
		f.Parent = root
		force = f
	end)
	tool.Unequipped:Connect(function()
		if force then
			force:Destroy()
			force = nil
		end
	end)
	tool.Parent = player:WaitForChild("Backpack")
end

local function applyVip(char: Model)
	local root = char:FindFirstChild("HumanoidRootPart") :: BasePart?
	local head = char:FindFirstChild("Head") :: BasePart?
	if root and not root:FindFirstChild("VipTrail") then
		local a0 = Instance.new("Attachment")
		a0.Position = Vector3.new(0, 1, 0)
		a0.Parent = root
		local a1 = Instance.new("Attachment")
		a1.Position = Vector3.new(0, -1, 0)
		a1.Parent = root
		local trail = Instance.new("Trail")
		trail.Name = "VipTrail"
		trail.Attachment0 = a0
		trail.Attachment1 = a1
		trail.Lifetime = 0.6
		trail.LightEmission = 1
		trail.Color = ColorSequence.new({
			ColorSequenceKeypoint.new(0, Color3.fromRGB(255, 60, 60)),
			ColorSequenceKeypoint.new(0.33, Color3.fromRGB(255, 220, 60)),
			ColorSequenceKeypoint.new(0.66, Color3.fromRGB(60, 220, 255)),
			ColorSequenceKeypoint.new(1, Color3.fromRGB(200, 80, 255)),
		})
		trail.Parent = root
	end
	if head and not head:FindFirstChild("VipTag") then
		local gui = Instance.new("BillboardGui")
		gui.Name = "VipTag"
		gui.Size = UDim2.fromOffset(100, 30)
		gui.StudsOffset = Vector3.new(0, 2.5, 0)
		gui.MaxDistance = 60
		local t = Instance.new("TextLabel")
		t.Size = UDim2.fromScale(1, 1)
		t.BackgroundTransparency = 1
		t.Font = Enum.Font.FredokaOne
		t.TextScaled = true
		t.Text = "VIP"
		t.TextColor3 = Color3.fromRGB(255, 210, 50)
		t.TextStrokeTransparency = 0
		t.Parent = gui
		gui.Parent = head
	end
end

function Perks.applyToCharacter(player: Player, char: Model)
	local hum = char:WaitForChild("Humanoid") :: Humanoid
	hum.WalkSpeed = baseSpeed(player)
	if owns(player, "VIP") then
		applyVip(char)
	end
	if owns(player, "SpeedCoil") then
		giveSpeedCoil(player)
	end
	if owns(player, "GravityCoil") then
		giveGravityCoil(player)
	end
end

-- Grants a pass for the current session and applies it to the live character.
local function grant(player: Player, key: string)
	if owns(player, key) then
		return
	end
	player:SetAttribute("Pass_" .. key, true)
	local char = player.Character
	if not char then
		return
	end
	if key == "VIP" then
		local hum = char:FindFirstChildOfClass("Humanoid")
		if hum then
			hum.WalkSpeed = Config.VipWalkSpeed
		end
		applyVip(char)
	elseif key == "SpeedCoil" then
		giveSpeedCoil(player)
	elseif key == "GravityCoil" then
		giveGravityCoil(player)
	end
end

function Perks.loadOwnership(player: Player)
	for _, key in Config.GamePassOrder do
		local id = Config.GamePasses[key].Id
		if id ~= 0 then
			local ok, result = pcall(MarketplaceService.UserOwnsGamePassAsync, MarketplaceService, player.UserId, id)
			if ok and result then
				player:SetAttribute("Pass_" .. key, true)
			end
		end
	end
end

function Perks.grantPassById(player: Player, passId: number): string?
	for _, key in Config.GamePassOrder do
		if Config.GamePasses[key].Id == passId and passId ~= 0 then
			grant(player, key)
			return key
		end
	end
	return nil
end

return Perks
