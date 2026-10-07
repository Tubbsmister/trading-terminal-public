--!strict
-- HUD: stage counter, Skip Stage button, shop for passes/products,
-- toasts, the "stuck? skip it" nudge and the play-again button.

local Players = game:GetService("Players")
local MarketplaceService = game:GetService("MarketplaceService")
local ReplicatedStorage = game:GetService("ReplicatedStorage")
local TweenService = game:GetService("TweenService")

local Config = require(ReplicatedStorage.Shared.Config)
local remotes = ReplicatedStorage:WaitForChild("Remotes")
local Notify = remotes:WaitForChild("Notify") :: RemoteEvent
local SkipNudge = remotes:WaitForChild("SkipNudge") :: RemoteEvent
local Restart = remotes:WaitForChild("Restart") :: RemoteEvent

local player = Players.LocalPlayer

local FONT = Enum.Font.FredokaOne
local GREEN = Color3.fromRGB(40, 200, 90)
local PURPLE = Color3.fromRGB(120, 70, 230)
local DARK = Color3.fromRGB(20, 18, 32)

local function new<T>(className: string, props: { [string]: any }, children: { Instance }?): T
	local inst = Instance.new(className)
	for k, v in props do
		if k ~= "Parent" then
			(inst :: any)[k] = v
		end
	end
	for _, c in children or {} do
		c.Parent = inst
	end
	if props.Parent then
		inst.Parent = props.Parent
	end
	return (inst :: any) :: T
end

local function corner(r: number): UICorner
	return new("UICorner", { CornerRadius = UDim.new(0, r) })
end

local function stroke(): UIStroke
	return new("UIStroke", { Thickness = 2, Color = Color3.new(0, 0, 0), Transparency = 0.4 })
end

local function button(text: string, color: Color3, props: { [string]: any }): TextButton
	local b = new("TextButton", {
		Text = text,
		Font = FONT,
		TextScaled = true,
		TextColor3 = Color3.new(1, 1, 1),
		BackgroundColor3 = color,
		AutoButtonColor = true,
	}, { corner(10), stroke(), new("UIPadding", {
		PaddingLeft = UDim.new(0, 8),
		PaddingRight = UDim.new(0, 8),
		PaddingTop = UDim.new(0, 4),
		PaddingBottom = UDim.new(0, 4),
	}) })
	for k, v in props do
		(b :: any)[k] = v
	end
	return b
end

-- Prices are looked up once and cached.
local priceCache: { [string]: number? } = {}
local function price(id: number, infoType: Enum.InfoType): number?
	local cacheKey = `{infoType.Name}:{id}`
	if priceCache[cacheKey] == nil then
		local ok, info = pcall(MarketplaceService.GetProductInfo, MarketplaceService, id, infoType)
		priceCache[cacheKey] = ok and info and info.PriceInRobux or nil
	end
	return priceCache[cacheKey]
end

local function robux(n: number?): string
	return n and `R${n}` or ""
end

local function buyProduct(key: string)
	local id = Config.Products[key].Id
	if id ~= 0 then
		MarketplaceService:PromptProductPurchase(player, id)
	end
end

local gui = new("ScreenGui", {
	Name = "Hud",
	ResetOnSpawn = false,
	IgnoreGuiInset = false,
	ZIndexBehavior = Enum.ZIndexBehavior.Sibling,
	Parent = player:WaitForChild("PlayerGui"),
}) :: ScreenGui

-- Stage counter
local stageLabel = new("TextLabel", {
	AnchorPoint = Vector2.new(0.5, 0),
	Position = UDim2.new(0.5, 0, 0, 8),
	Size = UDim2.fromOffset(240, 44),
	BackgroundColor3 = DARK,
	BackgroundTransparency = 0.25,
	Font = FONT,
	TextScaled = true,
	TextColor3 = Color3.new(1, 1, 1),
	Text = "Stage 1",
	Parent = gui,
}, { corner(12), new("UIPadding", { PaddingTop = UDim.new(0, 4), PaddingBottom = UDim.new(0, 4) }) }) :: TextLabel

-- Skip Stage: the main conversion button, always on screen.
local skipId = Config.Products.SkipStage.Id
local skipButton = button("SKIP STAGE", GREEN, {
	AnchorPoint = Vector2.new(1, 1),
	Position = UDim2.new(1, -12, 1, -110),
	Size = UDim2.fromOffset(170, 56),
	Visible = false,
	Parent = gui,
})
skipButton.Activated:Connect(function()
	buyProduct("SkipStage")
end)

local function refreshStage()
	local stage = (player:GetAttribute("Stage") :: number?) or 1
	stageLabel.Text = `Stage {stage} / {Config.StageCount}`
	skipButton.Visible = skipId ~= 0 and stage < Config.StageCount
end
player:GetAttributeChangedSignal("Stage"):Connect(refreshStage)
refreshStage()

task.spawn(function()
	if skipId ~= 0 then
		local p = price(skipId, Enum.InfoType.Product)
		if p then
			skipButton.Text = `SKIP STAGE {robux(p)}`
		end
	end
end)

-- Toasts
local toastHolder = new("Frame", {
	AnchorPoint = Vector2.new(0.5, 0),
	Position = UDim2.new(0.5, 0, 0, 60),
	Size = UDim2.new(0.6, 0, 0, 200),
	BackgroundTransparency = 1,
	Parent = gui,
}, { new("UIListLayout", {
	HorizontalAlignment = Enum.HorizontalAlignment.Center,
	Padding = UDim.new(0, 6),
}) })

local TOAST_COLORS = {
	stage = Color3.fromRGB(60, 160, 255),
	win = Color3.fromRGB(255, 190, 30),
	purchase = GREEN,
}

local function toast(text: string, kind: string)
	local t = new("TextLabel", {
		Size = UDim2.new(1, 0, 0, kind == "win" and 56 or 36),
		BackgroundColor3 = TOAST_COLORS[kind] or DARK,
		Font = FONT,
		TextScaled = true,
		TextColor3 = Color3.new(1, 1, 1),
		Text = text,
		Parent = toastHolder,
	}, { corner(10), stroke() }) :: TextLabel
	task.delay(kind == "win" and 6 or 2.5, function()
		local tween = TweenService:Create(t, TweenInfo.new(0.4), { BackgroundTransparency = 1, TextTransparency = 1 })
		tween:Play()
		tween.Completed:Wait()
		t:Destroy()
	end)
end
Notify.OnClientEvent:Connect(toast)

-- Shop
local shopFrame = new("Frame", {
	AnchorPoint = Vector2.new(0.5, 0.5),
	Position = UDim2.fromScale(0.5, 0.5),
	Size = UDim2.fromScale(0.5, 0.65),
	BackgroundColor3 = DARK,
	Visible = false,
	Parent = gui,
}, {
	corner(16),
	new("UISizeConstraint", { MinSize = Vector2.new(300, 300), MaxSize = Vector2.new(520, 560) }),
}) :: Frame

new("TextLabel", {
	Size = UDim2.new(1, -60, 0, 44),
	Position = UDim2.fromOffset(12, 8),
	BackgroundTransparency = 1,
	Font = FONT,
	TextScaled = true,
	TextXAlignment = Enum.TextXAlignment.Left,
	TextColor3 = Color3.new(1, 1, 1),
	Text = "SHOP",
	Parent = shopFrame,
})

local closeButton = button("X", Color3.fromRGB(220, 60, 70), {
	AnchorPoint = Vector2.new(1, 0),
	Position = UDim2.new(1, -10, 0, 10),
	Size = UDim2.fromOffset(40, 40),
	Parent = shopFrame,
})
closeButton.Activated:Connect(function()
	shopFrame.Visible = false
end)

local list = new("ScrollingFrame", {
	Position = UDim2.fromOffset(10, 60),
	Size = UDim2.new(1, -20, 1, -70),
	BackgroundTransparency = 1,
	BorderSizePixel = 0,
	ScrollBarThickness = 6,
	AutomaticCanvasSize = Enum.AutomaticSize.Y,
	CanvasSize = UDim2.new(),
	Parent = shopFrame,
}, { new("UIListLayout", { Padding = UDim.new(0, 8), SortOrder = Enum.SortOrder.LayoutOrder }) }) :: ScrollingFrame

local function shopRow(order: number, title: string, desc: string, color: Color3, onBuy: () -> (), ownedAttr: string?)
	local row = new("Frame", {
		LayoutOrder = order,
		Size = UDim2.new(1, -8, 0, 76),
		BackgroundColor3 = Color3.fromRGB(38, 34, 58),
		Parent = list,
	}, { corner(12) }) :: Frame
	new("TextLabel", {
		Position = UDim2.fromOffset(12, 6),
		Size = UDim2.new(0.65, -12, 0, 30),
		BackgroundTransparency = 1,
		Font = FONT,
		TextScaled = true,
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = color,
		Text = title,
		Parent = row,
	})
	new("TextLabel", {
		Position = UDim2.fromOffset(12, 38),
		Size = UDim2.new(0.65, -12, 0, 32),
		BackgroundTransparency = 1,
		Font = Enum.Font.Gotham,
		TextScaled = true,
		TextWrapped = true,
		TextXAlignment = Enum.TextXAlignment.Left,
		TextColor3 = Color3.fromRGB(200, 200, 220),
		Text = desc,
		Parent = row,
	})
	local buy = button("BUY", GREEN, {
		AnchorPoint = Vector2.new(1, 0.5),
		Position = UDim2.new(1, -10, 0.5, 0),
		Size = UDim2.new(0.33, -10, 0, 48),
		Parent = row,
	})
	buy.Activated:Connect(function()
		if not (ownedAttr and player:GetAttribute(ownedAttr)) then
			onBuy()
		end
	end)
	return buy
end

local order = 0
for _, key in Config.GamePassOrder do
	local pass = Config.GamePasses[key]
	if pass.Id ~= 0 then
		order += 1
		local attr = "Pass_" .. key
		local buy = shopRow(order, pass.Name, pass.Description, Color3.fromRGB(255, 210, 60), function()
			MarketplaceService:PromptGamePassPurchase(player, pass.Id)
		end, attr)
		local function refresh()
			if player:GetAttribute(attr) then
				buy.Text = "OWNED"
				buy.BackgroundColor3 = Color3.fromRGB(90, 90, 110)
			end
		end
		player:GetAttributeChangedSignal(attr):Connect(refresh)
		refresh()
		task.spawn(function()
			local p = price(pass.Id, Enum.InfoType.GamePass)
			if p and not player:GetAttribute(attr) then
				buy.Text = robux(p)
			end
		end)
	end
end
for _, key in Config.ProductOrder do
	local product = Config.Products[key]
	if product.Id ~= 0 then
		order += 1
		local buy = shopRow(order, product.Name, `Jump ahead {product.Stages} stage(s) instantly.`, Color3.fromRGB(120, 230, 150), function()
			buyProduct(key)
		end)
		task.spawn(function()
			local p = price(product.Id, Enum.InfoType.Product)
			if p then
				buy.Text = robux(p)
			end
		end)
	end
end

local shopButton = button("SHOP", PURPLE, {
	AnchorPoint = Vector2.new(0, 0.5),
	Position = UDim2.new(0, 12, 0.5, 0),
	Size = UDim2.fromOffset(110, 50),
	Visible = order > 0,
	Parent = gui,
})
shopButton.Activated:Connect(function()
	shopFrame.Visible = not shopFrame.Visible
end)

-- "Stuck?" nudge after repeated deaths on one stage
local nudge = new("Frame", {
	AnchorPoint = Vector2.new(0.5, 1),
	Position = UDim2.new(0.5, 0, 1, -20),
	Size = UDim2.fromOffset(340, 120),
	BackgroundColor3 = DARK,
	Visible = false,
	Parent = gui,
}, { corner(14), stroke() }) :: Frame
local nudgeText = new("TextLabel", {
	Position = UDim2.fromOffset(10, 8),
	Size = UDim2.new(1, -20, 0, 44),
	BackgroundTransparency = 1,
	Font = FONT,
	TextScaled = true,
	TextColor3 = Color3.new(1, 1, 1),
	Text = "Stuck?",
	Parent = nudge,
}) :: TextLabel
local nudgeSkip = button("SKIP IT", GREEN, {
	Position = UDim2.new(0, 10, 1, -58),
	Size = UDim2.new(0.5, -15, 0, 48),
	Parent = nudge,
})
local nudgeNo = button("Keep trying", Color3.fromRGB(80, 80, 100), {
	Position = UDim2.new(0.5, 5, 1, -58),
	Size = UDim2.new(0.5, -15, 0, 48),
	Parent = nudge,
})
nudgeSkip.Activated:Connect(function()
	nudge.Visible = false
	buyProduct("SkipStage")
end)
nudgeNo.Activated:Connect(function()
	nudge.Visible = false
end)
SkipNudge.OnClientEvent:Connect(function(stage: number)
	if skipId == 0 then
		return
	end
	local p = price(skipId, Enum.InfoType.Product)
	nudgeText.Text = if p then `Stuck on Stage {stage}? Skip it for {robux(p)}!` else `Stuck on Stage {stage}? Skip it!`
	nudge.Visible = true
	task.delay(8, function()
		nudge.Visible = false
	end)
end)

-- Play again after finishing
local again = button("PLAY AGAIN", Color3.fromRGB(255, 170, 30), {
	AnchorPoint = Vector2.new(0.5, 1),
	Position = UDim2.new(0.5, 0, 1, -30),
	Size = UDim2.fromOffset(220, 60),
	Visible = false,
	Parent = gui,
})
again.Activated:Connect(function()
	Restart:FireServer()
end)
player:GetAttributeChangedSignal("Completed"):Connect(function()
	again.Visible = player:GetAttribute("Completed") == true
end)
