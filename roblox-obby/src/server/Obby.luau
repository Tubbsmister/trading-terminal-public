--!strict
-- Procedurally builds the whole course at server start, so the place file
-- needs no hand-built parts. Stages spiral upward into a tower: each stage is
-- built in its own local frame along +Z, then the frame turns 90 degrees.

local RunService = game:GetService("RunService")
local Players = game:GetService("Players")

local Config = require(game:GetService("ReplicatedStorage").Shared.Config)

local Obby = {}

local PAD_SIZE = 12
local MIN_STAGE_RISE = 10

export type Course = {
	Checkpoints: { BasePart },
	Finish: BasePart,
}

type Ctx = {
	frame: CFrame,
	model: Model,
	color: Color3,
	rng: Random,
}

local function lerp(a: number, b: number, t: number): number
	return a + (b - a) * t
end

local function makePart(ctx: Ctx, size: Vector3, localCFrame: CFrame, props: { [string]: any }?): Part
	local p = Instance.new("Part")
	p.Anchored = true
	p.Size = size
	p.CFrame = ctx.frame * localCFrame
	p.Color = ctx.color
	p.Material = Enum.Material.SmoothPlastic
	p.TopSurface = Enum.SurfaceType.Smooth
	p.BottomSurface = Enum.SurfaceType.Smooth
	if props then
		for k, v in props do
			(p :: any)[k] = v
		end
	end
	p.Parent = ctx.model
	return p
end

-- Platform whose top surface is at y and whose back edge is at z.
local function platform(ctx: Ctx, size: Vector3, x: number, y: number, z: number, props: { [string]: any }?): Part
	return makePart(ctx, size, CFrame.new(x, y - size.Y / 2, z + size.Z / 2), props)
end

local function humanoidFromHit(hit: BasePart): Humanoid?
	local model = hit:FindFirstAncestorOfClass("Model")
	if model and Players:GetPlayerFromCharacter(model) then
		return model:FindFirstChildOfClass("Humanoid")
	end
	return nil
end

local function makeKill(part: BasePart)
	part.Color = Color3.fromRGB(255, 40, 60)
	part.Material = Enum.Material.Neon
	part:SetAttribute("Kill", true)
	part.Touched:Connect(function(hit)
		local hum = humanoidFromHit(hit)
		if hum and hum.Health > 0 then
			hum.Health = 0
		end
	end)
end

local function label(part: BasePart, text: string, color: Color3, maxDistance: number)
	local gui = Instance.new("BillboardGui")
	gui.Size = UDim2.fromOffset(220, 60)
	gui.StudsOffset = Vector3.new(0, 5, 0)
	gui.AlwaysOnTop = false
	gui.MaxDistance = maxDistance
	gui.Parent = part
	local t = Instance.new("TextLabel")
	t.Size = UDim2.fromScale(1, 1)
	t.BackgroundTransparency = 1
	t.Font = Enum.Font.FredokaOne
	t.TextScaled = true
	t.TextColor3 = color
	t.TextStrokeTransparency = 0.2
	t.Text = text
	t.Parent = gui
end

-- Spinning kill bars, driven from one loop. Overlap queries make the kill
-- reliable even though the bars are anchored and moved by CFrame.
local spinners: { { bar: BasePart, pivot: CFrame, speed: number } } = {}

local function startSpinnerLoop(course: Model)
	local params = OverlapParams.new()
	params.FilterType = Enum.RaycastFilterType.Exclude
	params.FilterDescendantsInstances = { course }
	RunService.Heartbeat:Connect(function()
		local t = os.clock()
		for _, s in spinners do
			s.bar.CFrame = s.pivot * CFrame.Angles(0, t * s.speed, 0)
			for _, hit in workspace:GetPartsInPart(s.bar, params) do
				local hum = humanoidFromHit(hit)
				if hum and hum.Health > 0 then
					hum.Health = 0
				end
			end
		end
	end)
end

-- Obstacle builders. Each takes a cursor (x, top y, front-edge z in the
-- stage's local frame) and returns the cursor after its last piece.
type Builder = (ctx: Ctx, cur: Vector3, diff: number) -> Vector3

local function jumps(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local rng = ctx.rng
	local x, y, z = cur.X, cur.Y, cur.Z
	local size = lerp(6, 3.5, diff)
	for _ = 1, rng:NextInteger(4, 6) do
		local gap = lerp(3.5, 7, diff) + rng:NextNumber(-0.5, 0.5)
		y += rng:NextNumber(-1, 2) * (1 - 0.4 * diff)
		x = math.clamp(x + rng:NextNumber(-3, 3), -8, 8)
		z += gap
		platform(ctx, Vector3.new(size, 1, size), x, y, z)
		z += size
	end
	return Vector3.new(x, y, z)
end

local function killBars(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local length = math.floor(lerp(30, 44, diff))
	local z0 = cur.Z + 3
	platform(ctx, Vector3.new(7, 1, length), cur.X, cur.Y, z0)
	local barHeight = lerp(1, 2.2, diff)
	local zb = z0 + 8
	while zb < z0 + length - 5 do
		local bar = platform(ctx, Vector3.new(7, barHeight, 1.5), cur.X, cur.Y + barHeight, zb)
		makeKill(bar)
		zb += lerp(10, 7.5, diff)
	end
	return Vector3.new(cur.X, cur.Y, z0 + length)
end

local function spinner(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local d = 26
	local center = CFrame.new(cur.X, cur.Y, cur.Z + 3 + d / 2)
	makePart(ctx, Vector3.new(1, d, d), center * CFrame.new(0, -0.5, 0) * CFrame.Angles(0, 0, math.rad(90)), {
		Shape = Enum.PartType.Cylinder,
	})
	local bar = makePart(ctx, Vector3.new(d - 2, 1, 1), center * CFrame.new(0, 1, 0))
	makeKill(bar)
	local hub = makePart(ctx, Vector3.new(1.2, 2, 2), center * CFrame.new(0, 1, 0) * CFrame.Angles(0, 0, math.rad(90)), {
		Shape = Enum.PartType.Cylinder,
		Color = Color3.fromRGB(30, 30, 40),
	})
	hub.CanCollide = false
	table.insert(spinners, {
		bar = bar,
		pivot = ctx.frame * center * CFrame.new(0, 1, 0),
		speed = lerp(1.4, 3, diff) * (ctx.rng:NextNumber() < 0.5 and -1 or 1),
	})
	return Vector3.new(cur.X, cur.Y, cur.Z + 3 + d)
end

local function vanishing(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local rng = ctx.rng
	local x, y, z = cur.X, cur.Y, cur.Z
	local delay = lerp(0.9, 0.45, diff)
	for _ = 1, rng:NextInteger(5, 7) do
		z += lerp(2.5, 4.5, diff)
		x = math.clamp(x + rng:NextNumber(-2, 2), -8, 8)
		local tile = platform(ctx, Vector3.new(5, 1, 5), x, y, z, {
			Material = Enum.Material.Neon,
			Transparency = 0.15,
		})
		local busy = false
		tile.Touched:Connect(function(hit)
			if busy or not humanoidFromHit(hit) then
				return
			end
			busy = true
			task.delay(delay, function()
				tile.Transparency = 0.85
				tile.CanCollide = false
				task.wait(2.5)
				tile.Transparency = 0.15
				tile.CanCollide = true
				busy = false
			end)
		end)
		z += 5
	end
	return Vector3.new(x, y, z)
end

local function truss(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local x, y = cur.X, cur.Y
	local z = cur.Z + 3
	platform(ctx, Vector3.new(8, 1, 6), x, y, z)
	z += 6
	local h = math.floor(lerp(14, 22, diff))
	local t = Instance.new("TrussPart")
	t.Anchored = true
	t.Size = Vector3.new(2, h, 2)
	t.CFrame = ctx.frame * CFrame.new(x, y + h / 2, z + 1)
	t.Color = ctx.color
	t.Parent = ctx.model
	platform(ctx, Vector3.new(8, 1, 8), x, y + h, z)
	return Vector3.new(x, y + h, z + 8)
end

local function beams(ctx: Ctx, cur: Vector3, diff: number): Vector3
	local rng = ctx.rng
	local x, y, z = cur.X, cur.Y, cur.Z
	local width = lerp(2.5, 1.2, diff)
	for _ = 1, 3 do
		z += lerp(2, 4, diff)
		x = math.clamp(x + rng:NextNumber(-1.5, 1.5), -8, 8)
		local len = rng:NextInteger(10, 14)
		platform(ctx, Vector3.new(width, 1, len), x, y, z)
		z += len
	end
	return Vector3.new(x, y, z)
end

local BUILDERS: { Builder } = { jumps, killBars, spinner, vanishing, truss, beams }

-- Easy steps up to guarantee each stage climbs above the one a lap below.
local function ascend(ctx: Ctx, cur: Vector3, targetY: number): Vector3
	local x, y, z = cur.X, cur.Y, cur.Z
	while y < targetY do
		z += 3
		y = math.min(y + 3, targetY)
		platform(ctx, Vector3.new(6, 1, 5), x, y, z)
		z += 5
	end
	return Vector3.new(x, y, z)
end

local function makePad(ctx: Ctx, index: number): Part
	local pad = makePart(ctx, Vector3.new(PAD_SIZE, 1, PAD_SIZE), CFrame.new(0, -0.5, 0), {
		Material = Enum.Material.Neon,
		Name = "Checkpoint" .. index,
	})
	pad:SetAttribute("Stage", index)
	label(pad, "Stage " .. index, Color3.new(1, 1, 1), 90)
	return pad
end

function Obby.build(): Course
	local model = Instance.new("Model")
	model.Name = "Course"
	model.Parent = workspace

	local rng = Random.new(Config.Seed)
	local ctx: Ctx = {
		frame = CFrame.new(0, 20, 0),
		model = model,
		color = Color3.new(1, 1, 1),
		rng = rng,
	}

	local checkpoints: { BasePart } = {}
	local lastBuilder: Builder? = nil

	for i = 1, Config.StageCount do
		local diff = (i - 1) / math.max(Config.StageCount - 1, 1)
		ctx.color = Color3.fromHSV(((i - 1) * 0.07) % 1, 0.75, 1)
		checkpoints[i] = makePad(ctx, i)

		-- Easy, flat obstacles for the first few stages so new players hook in.
		local builder: Builder
		if i <= 3 then
			builder = ({ jumps, killBars, beams } :: { Builder })[i]
		else
			repeat
				builder = BUILDERS[rng:NextInteger(1, #BUILDERS)]
			until builder ~= lastBuilder
		end
		lastBuilder = builder

		local cur = builder(ctx, Vector3.new(0, 0, PAD_SIZE / 2), diff)
		if i > 6 and diff > 0.3 then
			cur = BUILDERS[rng:NextInteger(1, #BUILDERS)](ctx, cur, diff * 0.8)
		end
		cur = ascend(ctx, cur, MIN_STAGE_RISE)

		-- Next pad: centre it ahead of the cursor, then turn the frame.
		local nextCenter = CFrame.new(cur.X, cur.Y, cur.Z + 4 + PAD_SIZE / 2)
		ctx.frame = ctx.frame * nextCenter * CFrame.Angles(0, math.rad(90), 0)
	end

	ctx.color = Color3.fromRGB(255, 205, 40)
	local finish = makePart(ctx, Vector3.new(20, 1, 20), CFrame.new(0, -0.5, 0), {
		Material = Enum.Material.Neon,
		Name = "Finish",
	})
	label(finish, "FINISH!", Color3.fromRGB(255, 230, 90), 200)

	-- Decorative lava sea under the tower; also a kill floor.
	local lava = Instance.new("Part")
	lava.Anchored = true
	lava.Size = Vector3.new(2048, 2, 2048)
	lava.CFrame = CFrame.new(0, -10, 0)
	lava.Parent = model
	makeKill(lava)

	startSpinnerLoop(model)

	return {
		Checkpoints = checkpoints,
		Finish = finish,
	}
end

return Obby
