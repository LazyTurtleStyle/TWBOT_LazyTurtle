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
    'bot': 'Set global bot configuration variables',
    'bot.active_hours': 'The hours when the bot should use active_delay (this does not impact attack timings)',
    'bot.delay_factor': 'Multiplier on the base 5-7 second action delay. Higher is slower and safer; very low values will probably cause a ban.',
    'bot.active_delay': 'Delay in seconds to use in bot active times',
    'bot.inactive_delay': 'Delay in seconds to use in bot inactive times',
    'bot.inactive_still_active': 'During inactive hours: when ON the bot waits inactive_delay between runs (slower, more human). When OFF it keeps running at the normal short interval even during inactive hours.',
    'bot.add_new_villages': 'Automatically add the default village config to newly conquered villages',
    'bot.village_name_template': 'Template to use for new villages, use {num} to set the config index as name',
    'bot.village_name_number_length': 'The number length, lower will be prefixed with zeroes',
    'bot.auto_set_village_names': 'Automatically set villages names',
    'bot.check_update': 'Check GitHub for a newer version of the bot on startup.',
    'bot.user_agent': 'Set this to the browser agent your session is using (otherwise could cause ban)',
    'bot.incoming_check': 'Run a background poller that tracks incoming attacks (origin, arrival, walking times, tagging) on its own schedule',
    'bot.incoming_check_min': 'Minimum seconds between incoming-attack checks (e.g. 300 = 5 min). Lower = more accurate tags but more requests',
    'bot.incoming_check_max': 'Maximum seconds between incoming-attack checks (e.g. 570 = 9.5 min)',
    'bot.claim_daily_bonus': 'Open the daily login-bonus chests automatically: once per day (during active hours) the bot visits the daily-bonus screen and claims every unlocked, uncollected chest. Locked chests and premium unlocks are never touched.',
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
    'market': 'Automatic management of market trading',
    'market.auto_trade': 'Enable automated trading',
    'market.max_trade_duration': 'Max duration of trades (hours)',
    'market.auto_remove': 'Automatically removes existing or expired trades',
    'market.trade_multiplier': 'Set to true if the world supports uneven trade ratios',
    'market.trade_multiplier_value': 'Trade ratio bias, only used when uneven ratios (trade_multiplier) are enabled. Lower values give you more resources per trade (1.0 = even).',
    'market.trade_max_per_hour': 'The amount of trades the bot can do in 1 hour',
    'market.trade_for_premium': 'Account-wide on/off switch for trading left-over resources for premium points. Also needs the per-village toggle enabled, and the world to actually have a premium market. Doing this too much could result in a ban.',
    'world.knight_enabled': 'The world has knights enabled',
    'world.flags_enabled': 'Capability marker only: does this world have flags? It no longer drives any behaviour - actual flag management lives on the Flags tab.',
    'world.quests_enabled': 'World has quests enabled (bot will automatically finish them)',
    'world.trade_for_premium': 'Capability marker only: does this world have a premium market? It no longer drives any behaviour - the on/off switch lives on the Market tab.',
    'world.archers_enabled': 'Are archers / marchers enabled on the world',
    'world.building_destruction_enabled': 'Are rams / catpults enabled on the world',
    'world.boosters_enabled': 'The world has resource/recruitment boosters (item boosts) enabled.',
    'flags': 'Manage village flags. Independent of the world capability marker - turn this on to let the bot keep a chosen flag assigned per village.',
    'flags.manage': 'Master switch for flag management. When ON the bot keeps each village\'s chosen flag (see the per-village flag_type) assigned, picking the highest level you own.',
    'flags.auto_upgrade': 'When ON, combine 3 flags of the same type/level into one of the next level. OFF by default so the bot never consumes your flags unless you ask.',
    'village.flag_type': 'Which flag to keep on this village: 1 Resource, 2 Recruitment, 3 Attack, 4 Defense, 5 Luck, 6 Population, 7 Coin cost, 8 Haul. Off = never assign a flag. Needs flag management enabled on the Flags tab.',
    'village_template': 'The default template for villages to use',
    'village.building': 'Override build template',
    'village.units': 'Override recruitment / farm template',
    'village.managed': 'The village should be managed by the bot',
    'village.scout_first': 'The village should scout villages before farming',
    'village.additional_farms': 'List of villages to include in the farming process (does not require find_player_owned to be active)',
    'village.prioritize_building': 'Do not recruit if the builder does not have enough resources',
    'village.prioritize_snob': 'Do not recruit if the snob does not have enough resources',
    'village.trade_for_premium': 'Trade this village\'s left-over resources for premium points (also requires the account-wide switch on the Market tab to be on).',
    'village.gather_enabled': 'Uses left-over units to gather additional resources if the option is enabled on the world',
    'village.gather_selection': 'The gather operation to preform (they have to be unlocked first)',
    'village.advanced_gather': 'Use a smarter scavenging split across the unlocked runs to maximise yield (only applies when gathering is enabled).',
    'village.gather_night_consolidate': 'Night mode: during the window below, send scavenging troops into one long run on the highest unlocked level instead of splitting, to cover an unattended night. The run is sized to be back home when the window ends; troops that don\'t fit go out on lower levels in later cycles. Turn off (or use the Scavenging quick-toggle) if you expect incoming attacks.',
    'village.gather_night_start': 'Hour (0-23) the night-consolidation window begins, e.g. 23.',
    'village.gather_night_end': 'Hour (0-23) the night-consolidation window ends, e.g. 7. Wraps past midnight when start > end.',
    'village.scavenge_unlock_enabled': 'Automatically unlock scavenging options (one at a time, lowest level first) once the headquarters reaches the level set below for each option.',
    'village.prioritize_scavenge_unlock': 'When an unlock is wanted but currently unaffordable, hold off building so resources accumulate for the unlock instead.',
    'village.scavenge_unlock_hq_1': 'Headquarters level at which scavenge option 1 should be unlocked (default 1 — it is nearly free, so unlock early).',
    'village.scavenge_unlock_hq_2': 'Headquarters level at which scavenge option 2 should be unlocked (default 5).',
    'village.scavenge_unlock_hq_3': 'Headquarters level at which scavenge option 3 should be unlocked (default 8).',
    'village.scavenge_unlock_hq_4': 'Headquarters level at which scavenge option 4 should be unlocked (default 15 — costly, so wait until the warehouse can hold it).',
    'village.snobs': 'The amount of snobs to create in the current village',
    'village.evacuate_fragile_units_on_attack': 'Automatically evacuate fragile units (axe, snob) to nearby safe villages in case of an attack',
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
  <div class="card-header bg-info text-white">How flag management works</div>
  <div class="card-body">
    <p class="mb-2 small">Turn on <b>manage</b> to let the bot keep a flag assigned to each
       village. The flag is chosen <b>per village</b> (set <b>flag_type</b> on the Default
       village template tab and on each village's own page). The bot always assigns the
       highest level of that flag type you own.</p>
    <p class="mb-1 small"><b>Flag types</b> (match the order shown on the in-game flags
       screen):</p>
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
    <small class="d-block text-muted mt-2"><b>auto_upgrade</b> is off by default, so the bot
      won't combine your flags into higher levels unless you enable it. Whether the world has
      flags at all is a separate marker on the World tab.</small>
  </div>
</div>
""",
}

# Group the settings inside a tab into labelled cards instead of one flat list.
# Each entry is a list of (group title, [parameter names]). Parameters not listed
# for a section fall into an automatic "Other" group at the bottom, so missing or
# newly added keys are never dropped.
config_groups = {
    'bot': [
        ('Timing & activity', ['active_hours', 'delay_factor', 'active_delay',
                               'inactive_delay', 'inactive_still_active',
                               'claim_daily_bonus']),
        ('New villages', ['add_new_villages', 'village_name_template',
                          'village_name_number_length', 'auto_set_village_names']),
        ('Incoming attacks', ['incoming_check', 'incoming_check_min', 'incoming_check_max']),
        ('Identity & updates', ['user_agent', 'check_update']),
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
        ('Barb shaper (alpha)', ['barb_shaper', 'shaper_min_wall', 'shaper_loss_tolerance',
                                'shaper_max_sends', 'shaper_ram_reserve']),
    ],
    'market': [
        ('Trading', ['auto_trade', 'auto_remove', 'max_trade_duration', 'trade_max_per_hour']),
        ('Uneven ratios', ['trade_multiplier', 'trade_multiplier_value']),
        ('Premium', ['trade_for_premium']),
    ],
    'village_template': [
        ('Templates', ['building', 'units']),
        ('Building', ['farm_priority_pop_pct']),
        ('Management', ['managed', 'scout_first', 'prioritize_building',
                       'prioritize_snob', 'snobs']),
        ('Farming', ['additional_farms']),
        ('Scavenging', ['gather_enabled', 'gather_selection', 'advanced_gather',
                       'gather_night_consolidate', 'gather_night_start', 'gather_night_end',
                       'scavenge_unlock_enabled', 'prioritize_scavenge_unlock',
                       'scavenge_unlock_hq_1', 'scavenge_unlock_hq_2',
                       'scavenge_unlock_hq_3', 'scavenge_unlock_hq_4']),
        ('Defence & support', ['evacuate_fragile_units_on_attack', 'support_others',
                              'support_others_factor', 'support_others_max_villages',
                              'request_support_on_attack']),
        ('Flags', ['flag_type']),
        ('Premium', ['trade_for_premium']),
    ],
    'flags': [
        ('Flag management', ['manage', 'auto_upgrade']),
    ],
}
