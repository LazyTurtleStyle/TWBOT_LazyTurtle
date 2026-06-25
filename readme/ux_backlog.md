# Web dashboard UX backlog

Captured from a review of the dashboard at `webmanager/`. Organized by the priority order
agreed on: (1) overview, (2) bot configuration, (3) farms/scavenging. "Set-once" items are
grouped at the end because they clutter the day-to-day flow.

Status legend: `[ ]` todo · `[~]` partially exists · `[x]` done

---

## P1 — Overview / status page

The landing page (`/`, `bot.html`) is the thing you look at most, but today it mostly shows
a raw dump of reports. Goal: an at-a-glance dashboard of what the bot is actually doing.

- [x] **Fix bot status indicator.** `BotManager.is_running()` now detects a running
  `python twb.py` process via `psutil` (precise match: a python process whose command runs
  a `twb.py` script), so the status reads "active" however the bot was launched, not only
  when the UI started it. `stop()` also targets the detected pid now.
- [x] **Real overview panel**: new `OverviewBuilder` (`webmanager/utils.py`) aggregates the
  cache dumps into summary cards (villages / farm & scout targets / scavenging runs / trades
  / build queue), total resources & troops, a per-village snapshot, and a recent-activity
  feed. Rendered on the status page (`/`, `bot.html`).
- [x] **Make the reports table meaningful.** Replaced the raw-JSON dump with a filtered
  activity feed: only `attack`/`scout` reports (origin → dest, loot, losses, time-ago),
  with `ScavengingCompletedReport`/`ReportTrade` rolled up into counters and achievement/
  system noise dropped.
  - Troops split into **Total troops** (owned, from `troops`) vs **Troops in villages**
    (home, from `available_troops`).
  - Reports now feed **newest-first** (`sync()` previously took the *oldest* 100, so the
    feed was always days stale), and a dedicated **Incoming** card surfaces attacks/scouts
    against us so routine farming can't bury them.
  - Fixed a Jinja gotcha: `resources.pop` resolved to the dict's `.pop` method — use
    `resources['pop']`.
  - Incoming split into **"Under attack now"** (live, from each village's `under_attack`
    flag; calm empty bar when clear) vs **"Recently hit by others"** (past, from reports) —
    so the loud alarm no longer fires for something that already happened.
  - **Build queue** shown as compact lettered chips (first = next up, highlighted) with the
    full building name + level on mouseover, plus a `+N` for the rest of the planned queue.
  - **Quick-settings side panel**: a fixed tab that opens green/red on-off toggles for the
    most-used settings (farming / recruiting / building / trading / scavenging). Writes to
    `config.json` via `/app/quick/set`; scavenging is broadcast to every village. Takes
    effect on the next bot cycle.
  - [x] **Target wall level in the feed.** Each farm/scout row in Recent activity shows a
    `wall N` badge sourced from the report's scouted `extra.buildings` (amber for wall >= 5,
    grey otherwise). A scouted village with no wall reads `wall 0`; never-scouted targets show
    no badge. `OverviewBuilder.build` adds `wall` to each activity item; rendered in `bot.html`.
  - [x] **Live incoming-attack tracking.** A background poller (`game/incomings.py`,
    `IncomingManager`) scrapes the in-game incomings screen every few minutes and caches each
    individual command (origin village/player, arrival time, first-seen) under
    `cache/incomings/`. "Under attack now" is now driven off this live, pruned cache with real
    per-attack countdowns, walking-time tables and a slowest-unit auto-tag estimate, instead of
    the coarse `under_attack` flag — which is only used as a fallback when the poller is disabled
    or logged out. Resilient to cookie expiry: detects logged-out scrapes, warns once via
    notification, persists the rotating session so the poller and main loop stop fighting over
    the `sid`, and the dashboard shows a "tracking is logged out" banner instead of a false
    all-clear.
  - [ ] Note: build-queue chips show the bot's *planned* template queue (`building_queue`),
    not the live in-game construction list (not cached separately).
- [x] **Wire up the session "Update" button.** It previously called an undefined
  `set_session_data()` with no backend route, so the cookie had to be hand-edited into
  `cache/cookies.txt`. Now pasting a cookie string and clicking Update parses it (same rules
  as `core/request.py`) and writes both `cache/session.json` (checked first) and
  `cache/cookies.txt` (fallback). Takes effect on next bot start, same as before. See
  `DataReader.session_set` + `POST /app/session/set`.

## Theming

- [x] **Light / Dark / TW theme switcher** in the navbar (all pages, base `main.html`).
  CSS-variable palettes; choice saved to `localStorage` and applied before paint to avoid
  flash. "TW" uses the parchment background `#ECD7AC`. Semantic colours (danger/warning
  rows, badges) are preserved across themes.

## P2 — Bot configuration UX

The config tabs (`/config`, `config.html`) expose every key as a flat list with terse,
sometimes vague help text. Goal: readable, well-explained settings.

- [x] **Better layout/grouping** of settings within each tab. Each tab is now split into
  labelled Bootstrap cards (`helpfile.config_groups`) instead of a flat `<hr>` list; every
  setting is a clean label + control row. Fields not assigned to a group fall into an
  automatic "Other" card so nothing is ever dropped. Applies to both `/config` and the
  per-village `/village` page (`render_grouped` in `webmanager/server.py`).
- [x] **Hover/mouseover explanations.** Each setting now has a `?` help badge with a
  Bootstrap tooltip sourced from `webmanager/helpfile.py` (`setting_row`), replacing the
  inline italics.
- [x] **Improved help text.** Rewrote the vague/broken entries (`bot.inactive_still_active`,
  `bot.delay_factor`, `market.trade_multiplier_value`, the reporting entries) and added the
  previously-undocumented keys (`notifications.*`, `bot.check_update`,
  `farms.forced_peace_times`, `village.advanced_gather`, `world.boosters_enabled`).
  - [ ] Still optional: move long-form help into a dedicated `help.md` / per-setting markdown
    so explanations can be richer than a single tooltip line.
- [x] **Clarified the "Reporting" tab.** Tab now reads **"Action log (export)"**
  (`helpfile.section_labels`) with a description spelling out that it's the bot's own action
  log/export (file or MySQL), *not* the in-game reports on the Status page.

## P3 — Farms & scavenging (high priority tab)

- [x] **Edit the in-game A/B farm templates from the dashboard.** New dedicated **Farms &
  scavenging** page (`/farms`, `farms.html`, linked in the navbar) surfaces
  `farms.template_id_scout` (A), `farms.template_id_minimal` (B) and
  `farms.template_minimal_troops` as editable inputs — these were previously `null`/dict in
  config and didn't even render on the config page. Grouped alongside the master switch,
  target selection, away times and the A/B/C risk & report-freshness settings, with inline
  help mirroring `readme/farm_checklist.md`. Writes via the existing `/app/config/set`.
- [x] **Scavenging settings** surfaced on the same page: account-wide `gather_enabled` /
  `gather_selection` (1–4) / `advanced_gather` controls that broadcast to every village +
  the template (new whitelisted `/app/scavenge/set` → `broadcast_village_set`), plus a
  per-village snapshot table linking to each village's config.
- [x] Farm/scavenge activity is already in the P1 overview — `OverviewBuilder` aggregates
  `farm_targets`, `scout_targets` and `scavenging_runs` counters, shown on the status page.
  - [ ] Optional polish: add "last run" timestamp and loot totals to those overview counters.

## Template editing (do-it-from-here)

Pattern request: create/edit templates inline instead of hand-writing `.txt` files.

- [x] **Building templates** — `/building_templates` editor is finished. Rows render
  client-side from the template file and are fully editable: change building (select) /
  target level inline, **Add row**, **Delete** row, reorder with up/down arrows, then
  **Save** (POST JSON → `/app/template/building/save`, which validates building names against
  the whitelist and drops bad rows before writing `building:level` lines). Also added
  **Delete template** (`/app/template/building/delete`). Writer/remover live in
  `BuildingTemplateManager.template_save` / `template_delete`. This is the model the unit and
  village editors should follow.
- [x] **Unit templates** — new form-based editor at `/unit_templates` (navbar + list/create/
  delete). Each troop template is edited as ordered **stages**: a gating building+level, a
  "recruit totals" table (pick unit + amount — grouped into the on-disk
  `build={building:{unit:amount}}` via the unit→building map), and an optional smith-upgrades
  table. Add/remove/reorder stages and rows, then Save (POST JSON → `/app/template/unit/save`,
  validated server-side). **Non-destructive**: legacy/extra stage keys (e.g. `farm`
  compositions) are round-tripped untouched — verified against all existing troop templates.
  Backend: `UnitTemplateManager` + `normalize_unit_stage`.
- [x] **Village templates** — a "village template" is not a separate file; it's the
  `village_template` settings block in `config.json` (which build/unit template a village uses
  + its behaviour toggles), already editable as a form on the **Default village template**
  config tab. Added the missing piece: an **apply-to-existing-villages** action, since the
  template otherwise only applies to newly added villages. New `/app/village/apply_template`
  (+ `DataReader.apply_village_template`) overwrites a village's settings with the defaults;
  surfaced as **"Reset to default"** on a village's config page, **"Default"** per row on the
  `/villages` list, and **"Reset all to default template"** for every village at once.

## Set-once configuration (group together, out of the daily flow)

These are configured once per world and shouldn't be in your way every session. Group them
into a "Setup / World" area, ideally a first-run wizard.

- [ ] **Group one-time setup** (server, reporting, notifications, world) into one setup
  flow, separate from the day-to-day Bot tab. Don't force re-entry on every start.
- [ ] **Support creating a fresh/empty bot for another world** (new config) from the UI —
  this is the scenario where the set-once server settings actually matter again.
- [x] **Notifications: add setup instructions.** The Notifications config tab now shows a
  step-by-step "Set up Telegram notifications" card (@BotFather token, add bot to chat, get
  chat id, enable + save) plus a **Send test message** button. Backend: data-driven
  `helpfile.section_setup` (rendered in `config.html`), `POST /app/notification/test`, and
  `Notification.test()` which sends a one-off message with the currently saved token/channel
  (ignores the `enabled` flag, no bot restart needed) and reports success/error inline.
- [ ] **World tab: `flags_enabled` should just declare "this is a flag world."** Right now
  enabling it also drives flag *upgrading*, which isn't always wanted. On the world tab it
  should be a pure capability toggle and do nothing else.

## Flag management (new feature — separate from the toggle above)

- [ ] **Dedicated flag-management options.** When flags exist, let the user choose behavior
  rather than always upgrading:
  - Toggle flag management on/off independently of `world.flags_enabled`.
  - Choose which flag to apply per village role, e.g. **resource (Grondstof) flags** for
    farm/resource villages, **recruitment flags** for troop-producing villages.
  - Don't auto-upgrade flags unless explicitly enabled.

## Market

- [ ] **Move `trade_for_premium` controls to the Market tab.** Keep only a capability flag
  on the World tab ("world has premium market"); the actual on/off behavior options belong
  with the other market settings.

---

## Notes / definitions (so we stop second-guessing these)

- **Status-page "Reports"** = parsed in-game reports from `cache/reports/` (farm/scout/attack
  results + system/achievement noise). The bot uses loot + wall level to pick farm templates.
- **Config "Reporting" tab** = action log/export to file or MySQL (`core/reporter.py`).
  Different thing from the above despite the name.
- **Notifications** = Telegram push (`core/notification.py`).
- **A / B / C farm logic** = re-scout / minimal / loot-exact, decided by report freshness.
  See `readme/farm_checklist.md`.
</content>
</invoke>
