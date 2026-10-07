--!strict
-- Central tuning + monetization IDs.
-- After publishing once, create the passes/products in Creator Hub
-- (Monetization > Passes / Developer Products) and paste their IDs here.
-- Any ID left at 0 is hidden from the shop, so the game runs fine without them.

local Config = {}

Config.GameName = "Neon Tower Obby"
Config.StageCount = 60
Config.Seed = 20261007 -- change to regenerate a different course layout

Config.GamePasses = {
	VIP = {
		Id = 0,
		Name = "VIP",
		Description = "Gold VIP tag, rainbow trail and +25% run speed forever.",
	},
	SpeedCoil = {
		Id = 0,
		Name = "Speed Coil",
		Description = "Equip to run 75% faster.",
	},
	GravityCoil = {
		Id = 0,
		Name = "Gravity Coil",
		Description = "Equip for super-high, floaty jumps.",
	},
}

-- Display order in the shop
Config.GamePassOrder = { "VIP", "SpeedCoil", "GravityCoil" }

Config.Products = {
	SkipStage = {
		Id = 0,
		Name = "Skip Stage",
		Stages = 1,
	},
	Skip5Stages = {
		Id = 0,
		Name = "Skip 5 Stages",
		Stages = 5,
	},
}

Config.ProductOrder = { "SkipStage", "Skip5Stages" }

-- Gameplay
Config.BaseWalkSpeed = 16
Config.VipWalkSpeed = 20
Config.SpeedCoilWalkSpeed = 28
Config.GravityCoilLift = 0.7 -- fraction of gravity cancelled while equipped
Config.DeathsBeforeSkipNudge = 3 -- deaths on one stage before suggesting a skip

return Config
