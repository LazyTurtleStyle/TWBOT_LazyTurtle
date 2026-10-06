# Snipe wave scripts (prototype)

Throwaway scripts from the nl116 noble wave of 2026-10-05 (~90 nobles, ~380
attacks, 2 villages lost). They drove the Snipe module from outside through the
dashboard's `/app/snipe/arm` and `/app/snipe/cancel` endpoints. Kept here as the
reference for building the same behaviour into the module; they hard-code the
world (`nl116`) and the dashboard on `127.0.0.1:5000`.

- `plan_all.py` - arms every free village on every open noble. Dry run by
  default, `--arm` to arm. Run from `worlds/<world>/`.
- `watch_cover.py` - polls `cache/snipes.json`; when a snipe keeps, cancels the
  other attempts it makes unnecessary.

## Feature spec: "Queue all options" on the Snipe tab

One button per noble train (and one for every tagged train) that arms every
reachable option and stops once the train is covered.

What the prototype taught us, as settings with their defaults:

| Setting | Default | Notes |
|---|---|---|
| Hits to keep per noble | 1 | 1-3. Stop cancelling the rest once this many keep. |
| Prefer the gap behind the first noble | on | A front-loaded first noble kills a support placed before it. A hit behind noble 1 covers the rest of a train with nothing between the nobles; a hit before it covers only noble 1. Plan order inside a train: noble 2, 3, 1, rest. |
| Keep window | whole gap, max ±50ms | Aim mid-gap between the previous command and the noble. Narrow gaps (an escort right before) shrink it. |
| Skip windows narrower than | ±3ms | Luck at ~±15-20ms network jitter. |
| Options per village | spear+sword (22), spear only (18), +1 catapult (30), heavy cav only (11, min 250) | Heavy cav is its own troop pool; a village can run both. Catapult pace exists to shift the send into a free slot. |
| Troops per attempt | sword up to 1000, spear up to 2000 total | Shortfall `scale`, min 80%: an attempt whose troops are kept elsewhere cancels itself. |
| Skip villages that are being nobled | on (toggle) | Their own troops stay home; also cancel snipes they are armed to send. |
| Untagged trains | last | Trains without the snipe tag are planned after tagged ones. |
| Spacing | 20-25s between any two sends, 2-3 min per village and troop pool | The runner needs ~15s per snipe (fire, read back, recall, prepare the next). |

Don't plan from villages whose troops are already kept in a snipe (per troop
pool: spear/sword vs heavy cav). Re-plan automatically after every cancel
round, so freed slots go to the trains that are still open.
