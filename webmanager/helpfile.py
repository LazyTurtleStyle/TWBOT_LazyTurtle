help_file = {
    'server': 'Configure the world the bot should play on',
    'server.server': 'The server endpoint to use (the world)',
    'server.endpoint': 'Endpoint server to use (the full url)',
    'server.server_on_twstats': 'Is the server listed on twstats.com?',
    'reporting': 'Action log / export of the bot\'s OWN activity to a file or database. This is NOT the in-game battle reports - those are shown on the Status page.',
    'reporting.enabled': 'Write a log of the actions the bot takes to the connection string below. An audit/export log, not in-game reports.',
    'reporting.connection_string': 'Where to write the action log: file://filename for a local file, or mysql://user:password@host:port/database_name for a database.',
    'notifications': 'Telegram push notifications for important events (e.g. incoming attacks). Requires a Telegram bot token and a channel id - see core/notification.py.',
    'notifications.enabled': 'Send push notifications to Telegram.',
    'notifications.channel_id': 'The Telegram chat/channel id the bot posts to.',
    'notifications.token': 'The Telegram bot API token, obtained from @BotFather.',
    'notifications.notify_startup': 'Notify when the bot starts up ("TWB is starting up").',
    'notifications.notify_crash': 'Notify when the bot crashes or gives up after repeated crashes.',
    'notifications.notify_session': 'Notify when the session/cookie is logged out (bot idle, or incoming attacks not being detected) and when it recovers. Recommended to keep on.',
    'notifications.notify_captcha': 'Notify when a captcha / bot protection is hit (bot paused until you solve it) and when it clears.',
    'notifications.notify_village': 'Notify about village/world changes: a village lost (conquered/nobled), an unreadable overview, or host-vs-server clock skew.',
    'notifications.notify_farm': 'Notify when a player-farm run auto-stops.',
    'notifications.notify_attack': 'Notify about attack-job outcomes: noble jobs, snipes and cancel-snipes.',
    'bot': 'Set global bot configuration variables',
    'bot.active_hours': 'The hours when the bot should use active_delay (this does not impact attack timings)',
    'bot.delay_factor': 'Multiplier on the base 5-7 second action delay. Higher is slower and safer; very low values will probably cause a ban.',
    'bot.active_delay': 'Delay in seconds to use in bot active times',
    'bot.inactive_delay': 'Delay in seconds to use in bot inactive times',
    'bot.inactive_still_active': 'During inactive hours: when ON the bot waits inactive_delay between runs (slower, more human). When OFF it keeps running at the normal short interval even during inactive hours.',
    'bot.add_new_villages': 'Automatically add the default village config to newly conquered villages',
    'bot.village_name_template': 'Template to use for new villages, use {num} to set the config index as name',
    'bot.village_name_number_length': 'The number length, lower will be prefixed with zeroes',
    'bot.village_name_number_start': 'The number the auto-namer gives the first village (default 1)',
    'bot.auto_set_village_names': 'Automatically set villages names',
    'bot.check_update': 'Check GitHub for a newer version of the bot on startup.',
    'bot.user_agent': 'Set this to the browser agent your session is using (otherwise could cause ban)',
    'bot.incoming_check': 'Run a background poller that tracks incoming attacks (origin, arrival, walking times, tagging) on its own schedule',
    'bot.incoming_check_min': 'Minimum seconds between incoming-attack checks (e.g. 300 = 5 min). Lower = more accurate tags but more requests',
    'bot.incoming_check_max': 'Maximum seconds between incoming-attack checks (e.g. 570 = 9.5 min)',
    'bot.clean_reports': 'Keep only this many of the newest reports, deleting older ones from cache/reports (0 = keep everything). The bot holds every cached report in memory, so an unbounded cache grows by roughly 3MB of RAM per 1000 reports. Note that farm loot averages are computed over the reports still cached, so a low cap makes farm profiling react faster but see less history.',
    'bot.farm_prune_days': 'Forget farm targets that have not been attacked in this many days (0 = never). Targets parked as unsafe are always kept.',
    'bot.claim_daily_bonus': 'Open the daily login-bonus chests automatically: once per day (during active hours) the bot visits the daily-bonus screen and claims every unlocked, uncollected chest. Locked chests and premium unlocks are never touched.',
    'account_manager': 'The in-game (premium) Account Manager. Say which jobs it is doing so the bot stops doing them too, and let the bot re-apply your group templates every morning - see the Account manager page.',
    'account_manager.enabled': 'Master switch. While off, the three switches below are ignored and the bot keeps doing everything itself, so a hand-over can be set up before it takes effect.',
    'account_manager.building': 'The Account Manager is building here, so the bot does not. Not the same as turning building off: it also lets the bot skip reading the main screen.',
    'account_manager.recruiting': 'The Account Manager is recruiting here, so the bot does not.',
    'account_manager.research': 'The Account Manager is researching here, so the bot does not.',
    'account_manager.auto_setup': 'Re-apply the group templates from the Account manager page once every morning (first cycle in active hours). Account Manager templates run out after a few days, this puts them back. Off means the plan is only applied when you press Apply now.',
    'defence': 'Defence: what the bot does about incoming attacks. Auto-dodge (alpha): rename an incoming attack so its name contains the trigger text (in game, or tag it from the Defense page) and the bot takes everything at home out just before it lands and brings it back seconds after. The Dodge tab on the Defense page shows what it is doing.',
    'defence.dodge': 'Dodge the incoming attacks whose name contains the trigger text. While OFF nothing leaves; a dodge that is already out is still brought home. The names are read from the game again in the minute the troops leave, so renaming an attack after it was planned still counts: take the trigger out and the dodge ends as "untagged" and the village blocks, rename it into the keep trigger and the blocker stays home after all.',
    'defence.dodge_trigger': 'The text that marks an incoming attack for dodging: its name only has to contain it, and upper/lower case does not matter. "DODGE THIS" matches "Ram >>>> DODGE THIS <<<<" (the unit in front is only the speed tag); use your own words or language, e.g. "dit ontwijken", and only names containing those are dodged.',
    'defence.dodge_leave_before_seconds': 'How long before the first tagged attack lands the troops leave. Shorter keeps them home longer, but leaves less time to retry if a send fails. Minimum 30.',
    'defence.dodge_return_after_seconds': 'How long after the last tagged attack lands the troops are back home.',
    'defence.dodge_keep_trigger': 'A second trigger: attacks whose name contains this are dodged too, but a small blocker stays home (the three numbers below) so fakes hit something instead of an empty village. If one trip also covers a plain dodge, everything leaves - a blocker left for a nuke only dies. A name matching both triggers uses the longer one.',
    'defence.dodge_keep_spear': 'Spears the keep-a-blocker dodge leaves home. Fewer at home than this means all of them stay.',
    'defence.dodge_keep_sword': 'Swords the keep-a-blocker dodge leaves home.',
    'defence.dodge_keep_spy': 'Scouts the keep-a-blocker dodge leaves home, so fake scouting runs are stopped too.',
    'defence.dodge_merge_seconds': 'Tagged attacks on the same village landing at most this far apart are dodged in one trip: out before the first, back after the last. One trip can last at most about twice the world\'s cancel time.',
    'minting': 'Coin minting: keep one village stocked so it can mint gold coins without you carrying resources by hand. See the Minting page.',
    'minting.enabled': 'Let the bot top up the coin village. Off by default; it only ever requests resources - it never mints, never switches on auto-minting and never touches a flag.',
    'minting.village': 'The village that mints. Put your coin-cost flag there and let the game auto-mint; this keeps its warehouse full.',
    'minting.group': 'Which village group to pull resources from ("0" is every village). Use a group when only the big villages should be emptied.',
    'minting.interval_minutes': 'How often to ask for resources. Hourly is plenty: merchants take time to arrive and the warehouse only holds so much.',
    'minting.ratio': '"coin" asks for resources in the proportions a coin actually costs, so nothing is wasted on a warehouse full of iron. "even" asks for equal amounts.',
    'minting.keep': 'Leave this much of each resource behind in every sending village, so they can still build and recruit.',
    'minting.min_send': 'Skip a village that could only spare less than this in total - not worth a merchant round trip.',
    'events': 'The rotating in-game events (the weekly themed mini-game). The bot notices whichever event is running from a page it already loads, and can play it for you - see the Events page.',
    'events.auto_play': 'Let the bot play the running event. Its energy bar refills on a timer and stops refilling once full, so every hour it sits capped is an action thrown away - this spends the bar down every cycle. Off by default; nothing is ever played until you switch it on.',
    'events.option': 'Which choice to make: "auto" picks whichever option is worth the most right now (the jackpots move, so the best choice moves with them), or set a number to always take that one.',
    'report_analysis': 'ALPHA. Reads your reports back out and writes what they mean onto the map. An attack that dies takes the attacker\'s nuke with it and rebuilding one takes weeks, so every defence that held is a fact about his next wave - that village cannot be in it. This finds those villages and writes the date onto each one as the private note the game keeps on its info page, where it is on screen the moment you click an incoming. It runs only when you press \"Let the bot write these\" on the Report analysis page: the bot writes that selection once, on its next cycle, and costs nothing the rest of the time. The settings here are the defaults the page starts from.',
    'report_analysis.min_units': 'How big an attack has to be before losing it counts as losing a clear. Below this it is a probe, a fake or snipe-bait rather than a nuke.',
    'report_analysis.min_loss_pct': 'How much of that attack has to have died. In practice this is near-binary - an attack either breaks on the wall or walks through it - so anything from about 80 upwards picks out the same set.',
    'report_analysis.alive_max_loss_pct': 'How little an attack can have lost and still count as a nuke that walked through - proof the village HAS one. The middle ground, a stack that lost most of itself but not all, is deliberately left out of both piles: it is neither gone nor intact, and guessing would put a wrong word on the map.',
    'report_analysis.note_prefix': 'What the note says about a village whose clear died. The village type goes in front and the date after it: "clear dood" gives "OFF - clear dood 12-09-2026". An older line written by this module is replaced, never stacked.',
    'report_analysis.note_prefix_alive': 'What the note says about a village whose clear is still alive, with the date it was last seen walking through.',
    'report_analysis.rebuild_days': 'How many days after a clear died it is probably back. A dead clear is not dead forever: past this date the Report analysis page marks it "rebuilt?" and the note says so, because the only way to know then is to scout it. 0 leaves the date off and never assumes.',
    'report_analysis.note_rebuild': 'Put in front of the rebuild date on a dead clear\'s note. "herbouwd ~" gives "OFF - clear dood 12-09-2026 (herbouwd ~26-09)".',
    'building.manage_buildings': 'Automatically manage buildings',
    'building': 'The automatic creation of buildings',
    'building.default': 'The default template to use, village configs override this variable',
    'building.max_lookahead': 'The max amount of items in queue to check before stopping',
    'building.max_queued_items': 'Max amount of queued items, default: 2 premium: 5',
    'building.farm_priority_pop_pct': 'Proactively queue a farm once population usage reaches this percent (e.g. 80 = at 80% full). 0 = off (only build farm when a building is actually blocked). Per-village setting can override this',
    'village.farm_priority_pop_pct': 'Population percent at which to proactively queue a farm for this village (e.g. 80). 0 = off, -1 = inherit the global building setting',
    'units': 'Enable automatic recruitment of units',
    'units.recruit': 'Automatically recruit units',
    'units.upgrade': 'Automatically upgrade units (only for level 1-3, 1-10 smith systems)',
    'units.default': 'The default template for unit creation (templates/troops)',
    'units.batch_size': 'The amount of units to attempt to create in a single run, increase this in late-game',
    'units.manage_defence': 'Manage defence between villages (experimental)',
    'units.remove_manual_queued': 'Remove manual queued recruitment entries',
    'units.randomize_unit_queue': 'Randomize unit queue, allows a more wide variety in units',
    'farms': 'Automatic farming of nearby (barbarian) villages',
    'farms.farm': 'Enable automatic farming',
    'farms.min_points': 'The minimum points of villages to attack (also checks custom_farms)',
    'farms.max_points': 'The maximum points of villages to attack (also checks custom_farms)',
    'farms.find_player_owned': 'Automatically attacks all player owned villages (dangerous)',
    'farms.search_radius': 'Max radius of villages to attack (fields)',
    'farms.default_away_time': 'Default time in seconds to sleep before attacking a village again',
    'farms.full_loot_away_time': 'Away time for villages with high resource gain',
    'farms.low_loot_away_time': 'Away time for villages with low resource gain',
    'farms.max_farms': 'The amount of nearby villages to check',
    'farms.attack_higher_points': 'If enabled villages with higher points than the current one will automatically be ignored',
    'farms.forced_peace_times': 'Time windows (e.g. night bonus / forced peace) during which the bot will not send farm attacks.',
    'farms.noble_barb': 'Master switch for the auto-noble engine (Attacks page, Noble barb tab). Jobs are configured and armed individually there; turning this off holds every job without disarming them.',
    'farms.noble_focus_fire': 'Take the targets one at a time, in the order they are listed on the Noble barb tab (use the arrows to reorder). The job at the top reserves every noble it could still need in the worst case - every hit rolling the 20 minimum - and only what is left over goes to the one below it. Off: every job takes whatever the overshoot guard allows, so several barbs get walked down at once and none of them finishes first.',
    'farms.mass_scavenge': 'ALPHA: scavenge a whole in-game village group in one pass on its own clock, using the game\'s own mass-scavenge screen (two page reads and one send for sixty villages) instead of waiting for each village\'s turn in the main loop. Villages in the group stop scavenging in their own run and follow these settings instead: which units, how many to keep home, which options, and roughly when the runs should be back. Night consolidation, skipping villages under attack and noble escort reservations all still apply. Configure it on the Farms page; ships disabled.',
    'farms.noble_escort_reserve': 'Keep the nobles and escort of an armed noble job home until the noble pass runs at the end of the cycle, instead of letting the barb shaper, scavenging and the player farms spend them first. Only reserves when the job is ready to send this cycle. The in-game Farm Assistant sizes its own sends, so a village whose reserve contains scouts or light cavalry skips its farm pass for that one cycle.',
    'farms.gather_group_policies':'Alpha: map of in-game village group (name or id) to a scavenging policy, "never" (troops always home, e.g. front def), "pause_attacked" (scavenge but always stop for incomings, e.g. mobile def) or "always" (keep scavenging even under attack, e.g. safe/rim def). A policy beats the per-village settings; a village in several policy groups gets the safest one (never > pause_attacked > always). Requires the incoming tracker (it caches the groups hourly).',
    'farms.template_id_scout': 'In-game Farm Assistant template id used to scout a target (find this by clicking the template\'s send button once in-game and checking the network request)',
    'farms.template_id_minimal': 'In-game Farm Assistant template id used as a minimal fallback attack when a fresh report has no loot info',
    'farms.template_minimal_troops': 'Troops the in-game minimal (B) template actually sends, e.g. {"light": 5, "spy": 1} - mirror this from the in-game template, there is no way to read it back automatically',
    'farms.minimal_loss_tolerance': 'Skip the minimal (B) or report (C) farm if the target\'s last known wall level is expected to cost us this many troops or more on average (real combat luck still varies per attack)',
    'farms.report_freshness_hours': 'A scout report this fresh or fresher is trusted for an exact loot-based attack (C); older but still valid reports fall back to the minimal template (B)',
    'farms.report_max_age_hours': 'A scout report older than this (or missing) is no longer trusted at all; the village is re-scouted (A) instead of attacked',
    'farms.barb_shaper': 'ALPHA: send axe+ram attacks to raze the walls of nearby barbs so farming stops bleeding light cavalry on them. Idle while scavenging uses axes; see the Barb shaper tab on the Farms page',
    'farms.shaper_min_wall': 'Barb shaper only targets barbs whose last scout report shows a wall higher than this level',
    'farms.shaper_loss_tolerance': 'Max expected troop deaths per shaping attack, estimated at worst-case luck (-25%); the axe escort is sized up until the estimate fits',
    'farms.shaper_max_sends': 'Max shaping attacks per village per farm cycle',
    'farms.shaper_ram_reserve': 'Rams that are never spent on wall shaping (kept home)',
    'farms.shaper_share_axes': 'Let the barb shaper run even when the scavenging unit picker includes axes; combine with farms.shaper_axe_cap so scavenging keeps the rest',
    'farms.shaper_axe_cap': 'Max axes the barb shaper may use per village per cycle, 0 = no limit; escorts below 20 axes are never sent',
    'farms.shaper_max_travel_hours': 'Skip shaping targets whose one-way ram travel time exceeds this many hours (0 = no limit)',
    'market': 'Automatic management of market trading',
    'balancer': 'Push resources from your big villages to your small ones. This is the in-game "send resources" screen between your own villages - no trade ratio, no counter-party. Amounts are always capped by how much room the RECEIVER has - counting deliveries that are still on the road, so several senders cannot each fill the same empty warehouse - and a rim village with a tiny warehouse never overflows.',
    'balancer.enabled': 'Turn resource balancing on. Off by default; nothing is sent until you enable it.',
    'balancer.sender_min_points': 'A village only gives resources away once it is at least this many points. Set it above your developed villages (e.g. 4000).',
    'balancer.receiver_max_points': 'A village only receives while it is under this many points. Once it grows past this it stops being fed automatically.',
    'balancer.target_fill_pct': 'Fill the receiver up to this percent of ITS OWN warehouse. 90 on a village with a 2788 cap means top it up to ~2509 of each resource. Scales automatically as the receiver builds storage.',
    'balancer.fill_mode': 'How one send is split over wood/stone/iron. BOTH settings start with the receiver\'s lowest resource - the difference is when they stop. "Stopping once it catches up" fills the lowest only until it matches the second lowest, then raises both together, so whatever is lowest right now always gets the most; the receiver ends up level. "All the way to the top" keeps pouring into that one resource until its warehouse ceiling, which overshoots past the other two and leaves the village lopsided until later sends correct it. Note that neither sends equal amounts - the amounts are deliberately uneven, that is what levels the receiver.',
    'balancer.target_order': 'Which needy village gets served first when merchants are limited. "nearest" = shortest merchant round-trip. "emptiest" = whoever is most starved, wherever they are.',
    'balancer.sender_order': 'When several big villages could feed the same receiver, which one does it. "nearest" = shortest merchant round-trip. "highest points" = your biggest village carries it. "most spare resources" = whoever is sitting on the largest pile, counted in raw resources, so 350k beats 20k regardless of warehouse size. "fullest warehouses" = whoever is nearest its own storage cap, which is the village actually about to waste production - a 400k village at 95% outranks a 500k one at 83%. A lower-ranked sender steps in only after the preferred one has had a turn and left the receiver unserved (so a sender with no free merchants cannot starve a receiver, and the run order cannot overrule your choice here).',
    'balancer.send_cooldown_minutes': 'Minimum minutes between sends from the same village. This is the "how often" knob.',
    'balancer.max_sends_per_receiver': 'Most deliveries a single RECEIVING village may get per cooldown window. 1 means every needy village gets topped up once per window. Several senders may still each deliver to the same village over time, but they share one budget: whatever is already walking towards it counts as delivered, so together they stop at the fill target instead of each filling it from scratch. A sender is not capped: it keeps serving needy villages until its merchants run out.',
    'balancer.reserve_merchants': 'Merchants to keep free for trading. Balancing and the Market tab compete for the same pool, so raise this if auto-trade starves.',
    'balancer.sender_keep': 'The sender always keeps at least this much of each resource, on top of whatever its own build/recruit queue is currently short of (that is held back automatically, per resource). Raise this if you want a buffer for queue items the village CAN already afford - those do not reserve anything by themselves.',
    'balancer.min_send_amount': 'Do not bother sending less than this of a resource. Stops merchants making near-empty trips.',
    'balancer.merchant_minutes_per_field': 'How long a merchant takes to cross one field on this world. Normally leave this empty: the bot reads the real trip time off the game\'s own transport confirmation screen and remembers it, and that measurement always wins over anything set here. It is only consulted for sends made before the first confirmation page has been read. Do not guess low - too low makes the bot forget convoys that are still travelling, which is the overshoot this whole mechanism exists to prevent.',
    'market.auto_trade': 'Enable automated trading',
    'market.max_trade_duration': 'Max duration of trades (hours)',
    'market.auto_remove': 'Clean up the bot\'s own offers once they pass Max trade duration. Offers you placed by hand are never removed.',
    'market.trade_multiplier': 'Set to true if the world supports uneven trade ratios',
    'market.trade_multiplier_value': 'Trade ratio bias, only used when uneven ratios (trade_multiplier) are enabled. Lower values give you more resources per trade (1.0 = even).',
    'market.trades_per_hour': 'How many trades the bot may start per hour, per village. Higher means more trading (2 = one every 30 minutes). Replaces Trade max per hour.',
    'market.max_trade_amount': 'Largest amount of a resource the bot will put in a single offer. Raise it to move a big shortfall in one go instead of over several trades. One merchant carries 1000, so the offer also needs that many merchants free.',
    'market.trade_max_per_hour': 'LEGACY, misleadingly named: this is the number of HOURS to wait between trades, so higher means LESS trading. Only used when Trades per hour is empty - set that one instead.',
    'market.trade_round_to_1000': 'Round offer amounts to the nearest 1000 (a merchant\'s carrying capacity). Non-round offers mostly get ignored by other players since they leave a merchant trip half-empty.',
    'market.trade_for_premium': 'Account-wide on/off switch for trading left-over resources for premium points. Also needs the per-village toggle enabled, and the world to actually have a premium market. Doing this too much could result in a ban.',
    'world.knight_enabled': 'The world has knights enabled',
    'world.flags_enabled': 'Capability marker only: does this world have flags? It no longer drives any behaviour - actual flag management lives on the Flags tab.',
    'world.quests_enabled': 'World has quests enabled (bot will automatically finish them)',
    'world.trade_for_premium': 'Capability marker only: does this world have a premium market? It no longer drives any behaviour - the on/off switch lives on the Market tab.',
    'world.archers_enabled': 'Are archers / marchers enabled on the world',
    'world.building_destruction_enabled': 'Are rams / catpults enabled on the world',
    'world.boosters_enabled': 'The world has resource/recruitment boosters (item boosts) enabled.',
    'flags': 'ALPHA. Manage village flags from a group -> flag type plan. Flags are an account-wide pool - you own a number of each type and level, and every one sits on exactly one village - so who gets what is a question about supply, and it is answered in one pass for the whole account. Build the plan on the Flags page; these are the switches it runs under.',
    'flags.manage': 'Master switch. While OFF nothing is ever assigned - the Flags page still reads your inventory, which is the way to look before handing them over. While ON, the plan is applied when you press Apply (and daily if auto_assign is on).',
    'flags.auto_assign': 'Re-apply the plan once a day on its own, on the first cycle in active hours. OFF by default: the normal way to use this is to build a plan and press Apply on the Flags page, so nothing moves a flag you did not ask it to.',
    'flags.auto_upgrade': 'When ON, combine 3 spare flags of the same type and level into one of the next level. OFF by default so the bot never consumes your flags unless you ask, and a flag standing on a village is never one of the three.',
    'flags.max_reads_per_run': 'How many village flag screens to read in one pass. Each is one request, and the reading is cached for half a day (a flag only moves when the bot moves it), so this only bites on the first run of a large account - which is then spread over a few cycles instead of going out as a burst.',
    'village.flag_type': 'Which flag this village keeps when NO row on the Flags page matches it: 1 Resource, 2 Recruitment, 3 Attack, 4 Defense, 5 Luck, 6 Population, 7 Coin cost, 8 Haul. Off = never assign a flag. A group row on the Flags page beats this.',
    'village_template': 'The default template for villages to use',
    'village.building': 'Build template for this village, or Off to construct nothing here (building levels are still read, so recruiting keeps working). Off only affects this village, the global Building master switch stays as it is.',
    'village.units': 'Recruitment / farm template for this village, or Off to recruit nothing here. Off only affects this village, the global Recruiting master switch stays as it is.',
    'village.managed': 'The village should be managed by the bot',
    'village.scout_first': 'The village should scout villages before farming',
    'village.farm_enabled': 'Send barb-farming attacks from this village (also requires the account-wide farms.farm master switch). Off just keeps this village\'s troops home; scavenging, building and defence are unaffected.',
    'village.additional_farms': 'List of villages to include in the farming process (does not require find_player_owned to be active)',
    'village.prioritize_building': 'Do not recruit if the builder does not have enough resources',
    'village.prioritize_snob': 'Do not recruit if the snob does not have enough resources',
    'village.trade_for_premium': 'Trade this village\'s left-over resources for premium points (also requires the account-wide switch on the Market tab to be on).',
    'village.gather_enabled': 'Uses left-over units to gather additional resources if the option is enabled on the world',
    'village.gather_selection': 'The gather operation to preform (they have to be unlocked first)',
    'village.advanced_gather': 'Use a smarter scavenging split across the unlocked runs to maximise yield (only applies when gathering is enabled).',
    'village.gather_when_attacked': 'Keep scavenging while this village has an incoming attack. Normally scavenging pauses under attack so troops stay home to defend; arm this manually when you judged the incoming harmless (e.g. a lone scout run), and turn it back off when a real attack is inbound. A group policy (farms.gather_group_policies) overrides this flag; night consolidation stays suspended while under attack regardless.',
    'village.gather_night_consolidate': 'Night mode: during the window below, send scavenging troops into one long run on the highest unlocked level instead of splitting, to cover an unattended night. The run is sized to be back home when the window ends; troops that don\'t fit go out on lower levels in later cycles. Turn off (or use the Scavenging quick-toggle) if you expect incoming attacks.',
    'village.gather_night_start': 'Hour (0-23) the night-consolidation window begins, e.g. 23.',
    'village.gather_night_end': 'Hour (0-23) the night-consolidation window ends, e.g. 7. Wraps past midnight when start > end.',
    'village.scavenge_unlock_enabled': 'Automatically unlock scavenging options (one at a time, lowest level first) once the headquarters reaches the level set below for each option.',
    'village.prioritize_scavenge_unlock': 'When an unlock is wanted but currently unaffordable, hold off building so resources accumulate for the unlock instead.',
    'village.scavenge_unlock_hq_1': 'Headquarters level at which scavenge option 1 should be unlocked (default 1, it is nearly free, so unlock early).',
    'village.scavenge_unlock_hq_2': 'Headquarters level at which scavenge option 2 should be unlocked (default 5).',
    'village.scavenge_unlock_hq_3': 'Headquarters level at which scavenge option 3 should be unlocked (default 8).',
    'village.scavenge_unlock_hq_4': 'Headquarters level at which scavenge option 4 should be unlocked (default 15, costly, so wait until the warehouse can hold it).',
    'village.snobs': 'The amount of snobs to create in the current village',
    'village.support_others': 'Allows the sending of automatic support',
    'village.support_others_factor': 'Factor of units to use in support operation (only defensive ones)',
    'village.support_others_max_villages': 'The max amount of villages to send support to (total 2 * 25% of troops)',
    'village.request_support_on_attack': 'Allows automatic requesting of support units'
}
buildings = ["main", "barracks", "stable", "watchtower", "smith", "garage", "place", "statue", "market", "wood",
             "stone", "iron", "farm", "hide", "wall", "snob", "church"]

# Maps each recruitable unit to the building it is produced from. Mirrors
# game/troopmanager.py:unit_building - used by the unit-template editor to group a
# stage's recruit amounts into the build={building: {unit: amount}} structure.
unit_building = {
    "spear": "barracks",
    "sword": "barracks",
    "axe": "barracks",
    "archer": "barracks",
    "spy": "stable",
    "light": "stable",
    "marcher": "stable",
    "heavy": "stable",
    "ram": "garage",
    "catapult": "garage",
}
unit_list = list(unit_building.keys())

# Friendlier display names for the config tabs. The raw section key (used as the
# tab anchor) stays the same; only the label shown to the user changes.
section_labels = {
    'server': 'Server / World',
    'reporting': 'Action log (export)',
    'notifications': 'Notifications (Telegram)',
    'bot': 'Bot',
    'building': 'Building',
    'units': 'Recruitment (units)',
    'farms': 'Farms',
    'market': 'Market',
    'balancer': 'Resource balancing',
    'defence': 'Defence',
    'report_analysis': 'Report analysis (alpha)',
    'world': 'World',
    'flags': 'Flags',
    'village_template': 'Default village template',
}

# Rich, multi-step setup guidance shown at the top of a config tab (rendered as
# raw HTML in config.html). Use for sections that need more than the one-line
# section help - e.g. wiring up an external service. Keys are section names.
section_setup = {
    'notifications': """
<div class="card config-card border-info">
  <div class="card-header bg-info text-white">Set up Telegram notifications</div>
  <div class="card-body">
    <p class="mb-2 small">The bot pushes important alerts (incoming attacks, session
       logged out) to a Telegram chat. One-time setup:</p>
    <ol class="small mb-2">
      <li>In Telegram, open <b>@BotFather</b>, send <code>/newbot</code>, follow the
          prompts, and copy the <b>API token</b> it gives you into
          <b>token</b> below.</li>
      <li>Create a channel or group (or just message your new bot directly), and
          <b>add the bot to it</b>. For a channel, make the bot an admin.</li>
      <li>Get the <b>chat id</b>: message <b>@userinfobot</b> (for your personal id) or
          <b>@getidsbot</b> in the target chat/channel, and copy the id into
          <b>channel_id</b> below. Channel ids usually start with <code>-100</code>.</li>
      <li>Set <b>enabled</b> to on, click <b>Save</b>, then use the test button below.</li>
    </ol>
    <button class="btn btn-sm btn-info" type="button" onclick="send_test_notification()">
      Send test message</button>
    <span id="notif_test_status" class="small ml-2"></span>
    <small class="d-block text-muted mt-1">The test uses the currently saved token /
      channel id, so Save first if you just changed them.</small>
  </div>
</div>
""",
    'flags': """
<div class="card config-card border-info">
  <div class="card-header bg-info text-white">How flags actually work</div>
  <div class="card-body">
    <p class="mb-2 small">A flag is not a per-village setting. Flags are an
       <b>account-wide inventory</b>: you own some number of them of each type and level,
       and every one you own sits on <b>exactly one village</b>. Giving village B a flag
       takes it off village A. You cannot have twenty villages on a resource flag while
       owning three of them.</p>
    <p class="mb-2 small">So who gets what is decided in one pass for the whole account, from
       a <b>group &rarr; flag type</b> plan you build on the
       <a href="/flags">Flags page</a>. Rows apply top to bottom and the later row wins:
       give every village the resource flag, then give [OFF] the attack flag. A village no
       row matches keeps its own <b>flag_type</b>. When there are not enough flags to go
       round, the more specific row is served first and the rest are listed as unmet &mdash;
       nothing is taken off a village that already has what it wants.</p>
    <p class="mb-1 small"><b>Flag types</b> (the order the in-game flags screen shows them):</p>
    <div class="row small">
      <div class="col-6"><ul class="mb-0">
        <li>1 &mdash; Resource production</li>
        <li>2 &mdash; Recruitment speed</li>
        <li>3 &mdash; Attack strength</li>
        <li>4 &mdash; Defense strength</li>
      </ul></div>
      <div class="col-6"><ul class="mb-0">
        <li>5 &mdash; Luck</li>
        <li>6 &mdash; Population</li>
        <li>7 &mdash; Reduce coin cost</li>
        <li>8 &mdash; Haul capacity</li>
      </ul></div>
    </div>
    <small class="d-block text-muted mt-2">Whether the world has flags at all is a separate
      marker on the World tab.</small>
  </div>
</div>
""",
}

# Group the settings inside a tab into labelled cards instead of one flat list.
# Each entry is a list of (group title, [parameter names]). Parameters not listed
# for a section fall into an automatic "Other" group at the bottom, so missing or
# newly added keys are never dropped.
config_groups = {
    'report_analysis': [
        ('What counts', ['min_units', 'min_loss_pct', 'alive_max_loss_pct',
                         'rebuild_days']),
        ('What to write', ['note_prefix', 'note_prefix_alive', 'note_rebuild']),
    ],
    'notifications': [
        ('Connection', ['enabled', 'token', 'channel_id']),
        ('Which messages to receive', ['notify_session', 'notify_captcha',
                                       'notify_crash', 'notify_village',
                                       'notify_farm', 'notify_attack',
                                       'notify_startup']),
    ],
    'bot': [
        ('Timing & activity', ['active_hours', 'delay_factor', 'active_delay',
                               'inactive_delay', 'inactive_still_active',
                               'claim_daily_bonus']),
        ('New villages', ['add_new_villages', 'village_name_template',
                          'village_name_number_length', 'village_name_number_start',
                          'auto_set_village_names']),
        ('Incoming attacks', ['incoming_check', 'incoming_check_min', 'incoming_check_max']),
        ('Identity & updates', ['user_agent', 'check_update']),
        ('Housekeeping', ['clean_reports', 'farm_prune_days']),
    ],
    'units': [
        ('Recruitment', ['recruit', 'default', 'batch_size', 'randomize_unit_queue',
                         'remove_manual_queued']),
        ('Upgrades & defence', ['upgrade', 'manage_defence']),
    ],
    'farms': [
        ('Master switch', ['farm']),
        ('Target selection', ['min_points', 'max_points', 'find_player_owned',
                             'search_radius', 'max_farms', 'attack_higher_points']),
        ('Timing', ['default_away_time', 'full_loot_away_time', 'low_loot_away_time',
                   'forced_peace_times']),
        ('In-game templates (A scout / B minimal)', ['template_id_scout',
                                                    'template_id_minimal',
                                                    'template_minimal_troops']),
        ('Risk & report freshness', ['minimal_loss_tolerance', 'report_freshness_hours',
                                    'report_max_age_hours']),
        ('Scavenging policy per group (alpha)', ['gather_group_policies']),
        ('Auto noble (alpha)', ['noble_barb', 'noble_escort_reserve',
                                'noble_focus_fire']),
        ('Barb shaper (alpha)', ['barb_shaper', 'shaper_min_wall', 'shaper_loss_tolerance',
                                'shaper_max_sends', 'shaper_ram_reserve',
                                'shaper_share_axes', 'shaper_axe_cap',
                                'shaper_max_travel_hours']),
    ],
    'market': [
        ('Trading', ['auto_trade', 'auto_remove', 'max_trade_duration', 'trades_per_hour', 'max_trade_amount', 'trade_max_per_hour', 'trade_round_to_1000']),
        ('Uneven ratios', ['trade_multiplier', 'trade_multiplier_value']),
        ('Premium', ['trade_for_premium']),
    ],
    'balancer': [
        ('On/off', ['enabled']),
        ('Who sends, who receives', ['sender_min_points', 'receiver_max_points', 'sender_order', 'target_order']),
        ('How much', ['target_fill_pct', 'fill_mode', 'sender_keep', 'min_send_amount']),
        ('How often', ['send_cooldown_minutes', 'max_sends_per_receiver', 'reserve_merchants', 'merchant_minutes_per_field']),
    ],
    'village_template': [
        ('Templates', ['building', 'units']),
        ('Building', ['farm_priority_pop_pct']),
        ('Management', ['managed', 'scout_first', 'prioritize_building',
                       'prioritize_snob', 'snobs']),
        ('Farming', ['farm_enabled', 'additional_farms']),
        ('Scavenging', ['gather_enabled', 'gather_selection', 'advanced_gather',
                       'gather_when_attacked',
                       'gather_night_consolidate', 'gather_night_start', 'gather_night_end',
                       'scavenge_unlock_enabled', 'prioritize_scavenge_unlock',
                       'scavenge_unlock_hq_1', 'scavenge_unlock_hq_2',
                       'scavenge_unlock_hq_3', 'scavenge_unlock_hq_4']),
        ('Defence & support', ['support_others',
                              'support_others_factor', 'support_others_max_villages',
                              'request_support_on_attack']),
        ('Flags', ['flag_type']),
        ('Premium', ['trade_for_premium']),
    ],
    'defence': [
        ('Dodge (alpha)', ['dodge', 'dodge_trigger', 'dodge_leave_before_seconds',
                           'dodge_return_after_seconds', 'dodge_merge_seconds']),
        ('Dodge, keep a blocker (alpha)', ['dodge_keep_trigger', 'dodge_keep_spear',
                                          'dodge_keep_sword', 'dodge_keep_spy']),
    ],
    'flags': [
        ('On/off', ['manage', 'auto_assign']),
        ('Upgrading', ['auto_upgrade']),
        ('Cost', ['max_reads_per_run']),
    ],
}

# Settings the bot no longer reads. Configs written before they were removed
# still carry them, and without this they would turn up unexplained under
# "Other" on the settings page, looking like switches that do something.
retired_settings = {
    # The stefan2200 evacuation: sent the off units away on any incoming and
    # never brought them back. Replaced by the tag-driven dodge (game/dodge.py).
    'evacuate_fragile_units_on_attack',
}
