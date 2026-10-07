# Neon Tower Obby

A 60-stage neon obby (obstacle course) that spirals up into a tower, with Robux monetization built in.
The whole course is generated from code when the server starts, so there are no hand-built parts to maintain.

**Gameplay:** jumps, kill-bar runways, spinning lasers, vanishing tiles, truss climbs and narrow beams. Difficulty ramps from stage 1 to 60. Checkpoints save to DataStores, and there's a Wins leaderboard plus Play Again for replays.

**Monetization:**

| Item | Type | Suggested price | What it does |
|---|---|---|---|
| Skip Stage | Developer Product (repeatable) | R$19 | Jump ahead one stage |
| Skip 5 Stages | Developer Product (repeatable) | R$79 | Jump ahead five stages |
| VIP | Game Pass | R$149 | Gold VIP tag, rainbow trail, +25% speed |
| Speed Coil | Game Pass | R$99 | Gear: +75% run speed while equipped |
| Gravity Coil | Game Pass | R$129 | Gear: floaty, much higher jumps |

Conversion hooks: a Skip Stage button that's always on screen, a "Stuck on Stage X? Skip it" popup after 3 deaths on the same stage, and a Shop button.
Premium Payouts are automatic: you earn from Premium players' playtime with no setup.

## Launch checklist (about 1–2 hours of your time)

You need a Roblox account and Roblox Studio (free, Windows or Mac).

1. **Open the place.** Download `NeonTowerObby.rbxlx` from this folder and open it in Roblox Studio.
2. **Test it.** Press **Play**. The tower builds in about a second. Check that you can clear the first few stages.
3. **Publish.** Go to **File → Publish to Roblox → Create new experience**. Name it something like "Neon Tower Obby 🌈" and set the genre to Obby/Platformer.
4. **Create the products.** In [Creator Hub](https://create.roblox.com/dashboard/creations), open the experience, then **Monetization**:
   - **Passes:** create VIP, Speed Coil and Gravity Coil. For each one, open it, choose Sales, turn on **Item for Sale** and set the price.
   - **Developer Products:** create Skip Stage and Skip 5 Stages, with prices.
   - Copy each item's numeric ID.
5. **Paste the IDs.** In Studio, open `ReplicatedStorage → Shared → Config` and replace each `Id = 0` with the matching ID. Then **File → Publish to Roblox** again. Items left at `0` stay hidden, so the game works without them.
6. **Go public.** In Creator Hub:
   - Fill in the **Maturity & Compliance questionnaire**. Roblox requires it before an experience can be public.
   - Upload an icon (512×512) and at least one thumbnail (1920×1080). Screenshots of the neon tower in Studio work.
   - Set **Privacy → Public**.
7. **Check it live.** Join the live game and buy Skip Stage once. Every purchase costs real Robux, including the owner's, so this proves the whole chain works. Robux from sales show as *pending* for a few days before they're spendable.

### Optional: publish from the command line

Use this after step 3 has created the experience. It updates the live game without opening Studio, which is handy for pushing Config changes.

1. Create an Open Cloud API key in Creator Hub with the `universe-places:write` scope for your experience.
2. Install [Rojo](https://rojo.space) and run:

```bash
ROBLOX_API_KEY=... UNIVERSE_ID=... PLACE_ID=... ./scripts/publish.sh
```

Place ID: the number in the game URL. Universe ID: shown in Creator Hub on the experience's overview page.

## Project layout (Rojo)

```
default.project.json          Rojo tree → builds NeonTowerObby.rbxlx
src/shared/Config.luau        Pass/product IDs, stage count, course seed, tuning
src/server/Main.server.luau   Stages, checkpoints, saving, ProcessReceipt, fall detection
src/server/Obby.luau          Procedural course generator (change Config.Seed for a new layout)
src/server/Perks.luau         Game pass ownership + VIP/coil perks
src/server/Data.luau          DataStore profile; receipt IDs stored with the profile so grants can't double-apply
src/client/Hud.client.luau    Stage counter, Skip button, shop, toasts, skip nudge, play again
```

Rebuild with `rojo build default.project.json -o NeonTowerObby.rbxlx`, or live-sync into Studio with `rojo serve`.

## Growth ideas after launch

- Spend R$100–500 on **Sponsored Experiences** (Creator Hub → Ads). This is the fastest way to get first players.
- Thumbnails and icons drive most clicks. Use bright, high-contrast images with a character mid-jump.
- Add more stages by raising `Config.StageCount`, then add a "stage 100" milestone badge.
