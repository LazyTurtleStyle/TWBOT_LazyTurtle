import collections
import json
import os
import sys
sys.path.insert(0, "../")

from flask import Flask, jsonify, request, render_template, redirect, Response

try:
    from webmanager.helpfile import (help_file, buildings, section_labels, config_groups,
                                     section_setup, unit_building, unit_list)
    from webmanager.utils import (DataReader, BotManager, MapBuilder, BuildingTemplateManager,
                                  UnitTemplateManager, OverviewBuilder, AttackPlanner,
                                  DefenseOverview, CSnipeOverview, SnipeOverview,
                                  PlayerFarmOverview)
except ImportError:
    from helpfile import (help_file, buildings, section_labels, config_groups,
                          section_setup, unit_building, unit_list)
    from utils import (DataReader, BotManager, MapBuilder, BuildingTemplateManager,
                       UnitTemplateManager, OverviewBuilder)

import datetime
from html import escape as html_escape

bm = BotManager()

app = Flask(__name__)
# Debug is enabled ONLY for local binds (see the __main__ block below). The
# Werkzeug interactive debugger runs arbitrary code on any unhandled exception,
# so it must never be on when the panel is reachable off-host - the dashboard
# has no authentication.
app.config["DEBUG"] = False

# Cookie holding the selected world ("" / absent = the default world).
WORLD_COOKIE = "twb_world"


@app.before_request
def _select_active_world():
    """Point DataReader at the world chosen via the navbar switcher (cookie).
    A ?world= query param overrides the cookie so cookie-less clients
    (the session-restore browser extension) can target a world."""
    DataReader.set_active_world(request.args.get("world") or request.cookies.get(WORLD_COOKIE))


@app.context_processor
def _inject_worlds():
    """Make the active world + world list + bot run-state available to every template."""
    return {
        "active_world": DataReader.active_world(),
        "worlds": DataReader.list_worlds(),
        "bot_running": bm.is_running(DataReader.active_world()),
        "session_expired": DataReader.session_logged_out(),
        "quick_toggles": quick_settings_state(),
    }


@app.route('/world/select')
def world_select():
    """Switch the dashboard to a world (stored in a cookie) and reload."""
    world = request.args.get("world", "")
    resp = redirect(request.referrer or "/")
    if world and world != "__default__":
        resp.set_cookie(WORLD_COOKIE, os.path.basename(world), max_age=60 * 60 * 24 * 365)
    else:
        resp.set_cookie(WORLD_COOKIE, "", expires=0)
    return resp


@app.template_filter('ts')
def format_timestamp(value):
    """Render a unix timestamp as a short local date/time, or '-' if missing."""
    if not value:
        return "-"
    try:
        return datetime.datetime.fromtimestamp(int(value)).strftime("%d %b %H:%M")
    except (ValueError, OSError, TypeError):
        return "-"


@app.template_filter('comma')
def format_comma(value):
    """Thousands-separated integer, or the original value if not numeric."""
    try:
        return "{:,}".format(int(value))
    except (ValueError, TypeError):
        return value


@app.template_filter('kshort')
def format_kshort(value):
    """Abbreviate big numbers: 1447 -> 1.4k, 35115 -> 35.1k, 1.2M -> 1.2m."""
    try:
        n = int(value)
    except (ValueError, TypeError):
        return value
    if abs(n) < 1000:
        return str(n)
    for div, suffix in ((1_000_000_000, 'b'), (1_000_000, 'm'), (1000, 'k')):
        if abs(n) >= div:
            out = "{:.1f}".format(n / div).rstrip('0').rstrip('.')
            return out + suffix
    return str(n)


@app.template_filter('dur')
def format_duration(value):
    """Render a number of seconds as H:MM:SS (or M:SS), '-' if missing."""
    try:
        seconds = int(value)
    except (ValueError, TypeError):
        return "-"
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return "%s%d:%02d:%02d" % (sign, hours, minutes, secs)
    return "%s%d:%02d" % (sign, minutes, secs)


@app.template_filter('ago')
def format_ago(value):
    """Render a unix timestamp as a compact 'time ago' string."""
    if not value:
        return ""
    try:
        delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(int(value))
    except (ValueError, OSError, TypeError):
        return ""
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return "%ds ago" % seconds
    if seconds < 3600:
        return "%dm ago" % (seconds // 60)
    if seconds < 86400:
        return "%dh ago" % (seconds // 3600)
    return "%dd ago" % (seconds // 86400)


def pre_process_bool(key, value, village_id=None):
    if village_id:
        if value:
            return '<button class="btn btn-sm btn-block btn-success" data-village-id="%s" data-type-option="%s" data-type="toggle">Enabled</button>' % (
            village_id, key)
        else:
            return '<button class="btn btn-sm btn-block btn-danger" data-village-id="%s" data-type-option="%s" data-type="toggle">Disabled</button>' % (
            village_id, key)
    if value:
        return '<button class="btn btn-sm btn-block btn-success" data-type-option="%s" data-type="toggle">Enabled</button>' % key
    else:
        return '<button class="btn btn-sm btn-block btn-danger" data-type-option="%s" data-type="toggle">Disabled</button>' % key


def preprocess_select(key, value, templates, village_id=None):
    output = '<select data-type-option="%s" data-type="select" class="form-control">' % key
    if village_id:
        output = '<select data-type-option="%s" data-village-id="%s" data-type="select" class="form-control">' % (
        key, village_id)

    for template in DataReader.template_grab(templates):
        output += '<option value="%s" %s>%s</option>' % (template, 'selected' if template == value else '', template)
    output += '</select>'
    return output


def pre_process_string(key, value, village_id=None):
    templates = {
        'units.default': 'templates.troops',
        'village.units': 'templates.troops',
        'building.default': 'templates.builder',
        'village_template.units': 'templates.troops',
        'village.building': 'templates.builder',
        'village_template.building': 'templates.builder'
    }
    if key in templates:
        return preprocess_select(key, value, templates[key], village_id)
    if village_id:
        return '<input type="text" class="form-control" data-village-id="%s" data-type="text" value="%s" data-type-option="%s" />' % (
        village_id, value, key)
    else:
        return '<input type="text" class="form-control" data-type="text" value="%s" data-type-option="%s" />' % (
            value, key)


def pre_process_number(key, value, village_id=None):
    if village_id:
        return '<input type="number" data-type="number" class="form-control" data-village-id="%s" value="%s" data-type-option="%s" />' % (
        village_id, value, key)
    return '<input type="number" data-type="number" class="form-control" value="%s" data-type-option="%s" />' % (
    value, key)


def pre_process_list(key, value, village_id=None):
    if village_id:
        return '<input type="text" data-type="list" class="form-control" data-village-id="%s" value="%s" data-type-option="%s" />' % (
        village_id, ', '.join(value), key)
    return '<input type="number" data-type="list" class="form-control" value="%s" data-type-option="%s" />' % (
    ', '.join(value), key)


# TribalWars flag type ids (stable across worlds) for the per-village flag_type
# dropdown. 0 = never assign a flag (only manage upgrades, if enabled).
FLAG_TYPE_OPTIONS = [
    (0, "Off (no flag assigned)"),
    (1, "Resource production"),
    (2, "Recruitment speed"),
    (3, "Attack strength"),
    (4, "Defense strength"),
    (5, "Luck"),
    (6, "Population"),
    (7, "Reduce coin cost"),
    (8, "Haul capacity"),
]
FIXED_SELECTS = {
    'village_template.flag_type': FLAG_TYPE_OPTIONS,
    'village.flag_type': FLAG_TYPE_OPTIONS,
}


def preprocess_fixed_select(key, value, options, village_id=None):
    """A <select> with a fixed (value, label) option list, e.g. flag_type."""
    vattr = (' data-village-id="%s"' % village_id) if village_id else ''
    out = '<select data-type-option="%s"%s data-type="select" class="form-control">' % (key, vattr)
    for val, label in options:
        out += '<option value="%s"%s>%s</option>' % (
            val, ' selected' if val == value else '', label)
    out += '</select>'
    return out


def template_names(category):
    """Available template names under templates/<category> (no extension)."""
    tdir = os.path.join(DataReader.project_root(), 'templates', category)
    try:
        return sorted(
            os.path.splitext(f)[0] for f in os.listdir(tdir)
            if not f.startswith('.') and os.path.isfile(os.path.join(tdir, f)))
    except OSError:
        return []


# The building/units fields hold a template name, or JSON false for "do
# nothing in this village" (per-village off-switch under the master switch).
TEMPLATE_SELECTS = {
    'village.building': 'builder', 'village_template.building': 'builder',
    'village.units': 'troops', 'village_template.units': 'troops',
}


def preprocess_template_select(key, value, category, village_id=None):
    """A <select> of the available templates + an Off option (stored as
    false). An unknown current value stays listed so it is not silently
    replaced on the next save of a different field."""
    known = template_names(category)
    current = value if isinstance(value, str) else None
    names = list(known)
    if current and current not in names:
        names.append(current)
    vattr = (' data-village-id="%s"' % village_id) if village_id else ''
    label = 'Off — do not %s in this village' % (
        'build' if category == 'builder' else 'recruit')
    out = '<select data-type-option="%s"%s data-type="select" class="form-control">' % (key, vattr)
    out += '<option value="false"%s>%s</option>' % (
        ' selected' if value is False else '', label)
    for name in names:
        out += '<option value="%s"%s>%s%s</option>' % (
            html_escape(name, quote=True), ' selected' if name == current else '',
            html_escape(name), '' if name in known else ' (missing template!)')
    out += '</select>'
    return out


def control_for(key, value, village_id=None):
    if key in TEMPLATE_SELECTS:
        return preprocess_template_select(key, value, TEMPLATE_SELECTS[key], village_id)
    if key in FIXED_SELECTS:
        return preprocess_fixed_select(key, value, FIXED_SELECTS[key], village_id)
    # bool is a subclass of int, so it must be checked first.
    if isinstance(value, bool):
        return pre_process_bool(key, value, village_id)
    if isinstance(value, str):
        return pre_process_string(key, value, village_id)
    if isinstance(value, list):
        return pre_process_list(key, value, village_id)
    if isinstance(value, (int, float)):
        return pre_process_number(key, value, village_id)
    return ''


def setting_row(key, control_html):
    """Render a single setting as a label + help tooltip + control row."""
    name = key.split('.')[-1] if '.' in key else key
    name = (name[0].upper() + name[1:]).replace('_', ' ')
    # village_template.* settings reuse the village.* help entries.
    help_txt = help_file.get(key.replace('village_template', 'village'), '')
    help_icon = ''
    if help_txt:
        help_icon = ('<span class="config-help" data-toggle="tooltip" '
                     'data-placement="top" title="%s">?</span>' % html_escape(help_txt, quote=True))
    return ('<div class="config-row">'
            '<div class="config-row-label"><span class="config-row-name">%s</span>%s</div>'
            '<div class="config-row-control">%s</div>'
            '</div>') % (name, help_icon, control_html)


def render_card(title, body):
    header = '<div class="card-header">%s</div>' % title if title else ''
    return '<div class="card config-card">%s<div class="card-body">%s</div></div>' % (header, body)


def render_grouped(group_key, ctrl_prefix, fields, village_id=None):
    """Render a config section's fields as grouped cards (helpfile.config_groups)."""
    rows = {}
    for parameter, value in fields.items():
        kvp = '%s.%s' % (ctrl_prefix, parameter)
        rows[parameter] = setting_row(kvp, control_for(kvp, value, village_id))

    groups = config_groups.get(group_key)
    if not groups:
        return render_card(None, ''.join(rows.values()))

    out = ''
    used = set()
    for title, params in groups:
        body = ''.join(rows[p] for p in params if p in rows)
        used.update(p for p in params if p in rows)
        if body:
            out += render_card(title, body)
    leftover = ''.join(rows[p] for p in rows if p not in used)
    if leftover:
        out += render_card('Other', leftover)
    return out


def _example_defaults():
    """Sections/keys from config.example.json. Merged (display-only) into the
    settings pages so options added to the bot after a world's config.json was
    created still render with their default value - otherwise a new setting is
    invisible until the key is added to the file by hand. Saving one persists
    it to the world config via the normal config_set path."""
    path = os.path.join(DataReader.project_root(), "config.example.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _with_defaults(fields, example_section):
    merged = dict(fields)
    for key, default in (example_section or {}).items():
        merged.setdefault(key, default)
    return merged


def pre_process_config():
    config = sync()['config']
    example = _example_defaults()
    to_hide = ["build", "villages"]
    sections = {}
    for section in config:
        if section in to_hide or not isinstance(config[section], dict):
            continue
        fields = _with_defaults(config[section], example.get(section))
        sections[section] = render_grouped(section, section, fields)
    return sections


# The configure-once-per-world sections, in the order a first-run wizard walks
# them. Grouped here so they're out of the day-to-day Bot tab.
SETUP_SECTIONS = ['server', 'world', 'notifications', 'reporting']


def pre_process_setup():
    """Render the set-once sections as (section, html) steps for the setup page."""
    config = sync()['config']
    example = _example_defaults()
    steps = []
    for section in SETUP_SECTIONS:
        if section in config and isinstance(config[section], dict):
            fields = _with_defaults(config[section], example.get(section))
            steps.append((section, render_grouped(section, section, fields)))
    return steps


def pre_process_village_config(village_id):
    config = sync()['config']
    template = config.get('village_template', {}) or {}
    villages = config['villages']
    if village_id in villages:
        vcfg = villages[village_id]
    else:
        vcfg = villages[list(villages.keys())[0]]
    # Display-only merge: show every template field, using the village's own
    # value where it has one. Lets template keys added after a village was
    # saved (e.g. farm_priority_pop_pct) still render. Nothing is persisted
    # until the user actually edits a field.
    merged = dict(template)
    if isinstance(vcfg, dict):
        merged.update(vcfg)
    # Backfill keys added to the example template after this world's config was
    # written (e.g. new scavenge unlock options) so they still render in
    # settings before the bot's next config merge persists them.
    for key, value in DataReader.example_village_template().items():
        merged.setdefault(key, value)
    return render_grouped('village_template', 'village', merged, village_id)


def sync():
    reports = DataReader.cache_grab("reports")
    villages = DataReader.cache_grab("villages")
    attacks = DataReader.cache_grab("attacks")
    config = DataReader.config_grab()
    managed = DataReader.cache_grab("managed")
    bot_status = bm.is_running(DataReader.active_world())

    # Newest reports first (higher id == more recent), keep the latest 100.
    sort_reports = {key: value for key, value in
                    sorted(reports.items(), key=lambda item: int(item[0]), reverse=True)}
    n_items = {k: sort_reports[k] for k in list(sort_reports)[:100]}

    out_struct = {
        "attacks": attacks,
        "villages": villages,
        "config": config,
        "reports": n_items,
        "bot": managed,
        "status": bot_status
    }
    return out_struct


@app.route('/api/get', methods=['GET'])
def get_vars():
    return jsonify(sync())


@app.route('/bot/start')
def start_bot():
    world = DataReader.active_world()
    bm.start(world)
    return jsonify(bm.is_running(world))


@app.route('/bot/stop')
def stop_bot():
    world = DataReader.active_world()
    bm.stop(world)
    return jsonify(not bm.is_running(world))


@app.route('/app/world/create', methods=['POST'])
def world_create():
    """Create a new world's config from the dashboard, optionally starting it."""
    result = DataReader.create_world(
        request.form.get("url", ""),
        request.form.get("user_agent", ""),
        request.form.get("cookie", ""),
    )
    if not result.get("ok"):
        return jsonify(result)
    start = str(request.form.get("start", "")).lower() in ("1", "true", "on", "yes")
    if start:
        bm.start(result["world"])
    result["started"] = bool(start)
    return jsonify(result)


@app.route('/config', methods=['GET'])
def get_config():
    return render_template('config.html', data=sync(), config=pre_process_config(),
                           helpfile=help_file, section_labels=section_labels,
                           section_setup=section_setup)


def noble_overview(data):
    """Noble jobs + the source-village picker data for the noble tab."""
    managed = data.get('bot', {}) or {}
    sources = []
    for vid, v in managed.items():
        pub = (v or {}).get('public') or {}
        loc = pub.get('location')
        sources.append({
            "id": str(vid),
            "name": v.get('name') or pub.get('name') or vid,
            "coord": "%s|%s" % (loc[0], loc[1]) if loc and len(loc) == 2 else "",
            "troops": (v or {}).get('available_troops') or {},
        })
    sources.sort(key=lambda s: str(s["name"]))
    jobs = sorted(
        DataReader.noble_grab(),
        key=lambda j: ({"armed": 0, "paused": 1, "stopped": 2, "done": 3}
                       .get(j.get("status"), 9), -(j.get("created") or 0)))
    return {"jobs": jobs, "sources": sources}


@app.route('/attacks', methods=['GET'])
def attacks_page():
    data = sync()
    scheduled = sorted(
        DataReader.schedule_grab(),
        key=lambda c: (c.get("status") != "pending", c.get("send_ts") or 0),
    )
    return render_template('attacks.html', data=data,
                           plan=AttackPlanner.build(data), scheduled=scheduled,
                           noble=noble_overview(data))


@app.route('/app/noble/add', methods=['POST'])
def noble_add():
    """Create an auto-noble job. Expects JSON: source_id, target_x, target_y,
    escort {unit: count}, escort_min_pct. The job starts paused (disarmed)."""
    body = request.get_json(silent=True) or {}
    entry, error = DataReader.noble_add(
        target_x=body.get("target_x"),
        target_y=body.get("target_y"),
        source_id=body.get("source_id"),
        escort=body.get("escort") or {},
        escort_min_pct=body.get("escort_min_pct", 80),
    )
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "entry": entry})


@app.route('/app/noble/toggle', methods=['GET', 'POST'])
def noble_toggle():
    jid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    state = DataReader.noble_toggle(jid)
    return jsonify({"ok": state is not None, "status": state})


@app.route('/app/noble/remove', methods=['GET', 'POST'])
def noble_remove():
    jid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    return jsonify({"ok": DataReader.noble_remove(jid)})


@app.route('/app/attack/schedule', methods=['POST'])
def attack_schedule():
    """Queue a timed attack. Expects JSON: origin_id, target_x, target_y,
    arrival (unix seconds), units {unit: count}."""
    body = request.get_json(silent=True) or {}
    entry, error = DataReader.schedule_create(
        origin_id=body.get("origin_id"),
        target_x=body.get("target_x"),
        target_y=body.get("target_y"),
        units=body.get("units") or {},
        arrival_ts=body.get("arrival"),
    )
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "entry": entry})


@app.route('/app/attack/schedule/cancel', methods=['GET', 'POST'])
def attack_schedule_cancel():
    cid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    return jsonify({"ok": DataReader.schedule_cancel(cid)})


@app.route('/defense', methods=['GET'])
def defense_page():
    data = sync()
    return render_template('defense.html', data=data,
                           defense=DefenseOverview.build(data),
                           csnipe=CSnipeOverview.build(data),
                           snipe=SnipeOverview.build(data))


@app.route('/app/csnipe/arm', methods=['POST'])
def csnipe_arm():
    """Arm a cancel snipe. Expects JSON: village_id, incoming_id, first_hit_ms
    (epoch ms of the train's first hit), aim_ms (return this many ms after it),
    lead_min, target_x, target_y, units {unit: count}, test (dry run against a
    chosen return moment instead of an incoming), window_ms (optional: the
    return must land at most this many ms past the target - the engine
    re-fires missed sends until one is inside the window)."""
    body = request.get_json(silent=True) or {}
    entry, error = DataReader.csnipe_arm(
        village_id=body.get("village_id"),
        incoming_id=body.get("incoming_id"),
        first_hit_ms=body.get("first_hit_ms"),
        aim_ms=body.get("aim_ms"),
        lead_min=body.get("lead_min"),
        target_x=body.get("target_x"),
        target_y=body.get("target_y"),
        units=body.get("units") or {},
        test=bool(body.get("test")),
        window_ms=body.get("window_ms"),
    )
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "entry": entry})


@app.route('/app/csnipe/cancel', methods=['GET', 'POST'])
def csnipe_cancel():
    sid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    state = DataReader.csnipe_disarm(sid)
    return jsonify({"ok": bool(state), "state": state})


@app.route('/app/snipe/arm', methods=['POST'])
def snipe_arm():
    """Arm one support-snipe per selected option. Expects JSON: incoming_id,
    target_village_id, land_ms (epoch ms the support must land), options
    [{village_id, pace_unit, units {unit: count}}], shortfall, min_pct, boost."""
    body = request.get_json(silent=True) or {}
    armed, errors = DataReader.snipe_arm_batch(
        incoming_id=body.get("incoming_id"),
        target_village_id=body.get("target_village_id"),
        land_ms=body.get("land_ms"),
        options=body.get("options") or [],
        shortfall=body.get("shortfall"),
        min_pct=body.get("min_pct"),
        boost=body.get("boost"),
    )
    return jsonify({"ok": bool(armed), "armed": len(armed), "errors": errors})


@app.route('/app/snipe/cancel', methods=['GET', 'POST'])
def snipe_cancel():
    sid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    state = DataReader.snipe_disarm(sid)
    return jsonify({"ok": bool(state), "state": state})


@app.route('/setup', methods=['GET'])
def setup_page():
    return render_template('setup.html', data=sync(), sections=pre_process_config(),
                           helpfile=help_file, section_labels=section_labels,
                           section_setup=section_setup)


@app.route('/app/notification/test', methods=['POST'])
def notification_test():
    """Send a test Telegram message using the selected world's saved config."""
    try:
        from core.notification import Notification
        # Pass the active world's config explicitly: the web process's
        # FileManager is not world-aware (only DataReader is), so letting
        # Notification.test() read config.json itself would hit the wrong file.
        ok, err = Notification.test(config=DataReader.config_grab())
    except Exception as exc:  # e.g. telegram lib missing
        ok, err = False, str(exc)
    return jsonify({"ok": ok, "error": err})


def _warehouse_capacity(level):
    """Standard TribalWars warehouse capacity for a storage level (caps at 400k)."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 1000
    return min(400000, int(round(1000 * (1.2294934 ** (level - 1))))) if level else 1000


def _farm_capacity(level):
    """Standard TribalWars farm population capacity for a farm level (caps at 24k)."""
    try:
        level = int(level)
    except (TypeError, ValueError):
        return 240
    return min(24000, int(round(240 * (1.172103 ** (level - 1))))) if level else 240


def pre_process_village_detail(data, vid):
    """Dashboard view-model for one village (resources + capacity, troops, queue)."""
    vd = (data.get('bot', {}) or {}).get(str(vid)) or {}
    public = vd.get('public', {}) or {}
    res = vd.get('resources', {}) or {}
    levels = vd.get('buidling_levels', {}) or {}
    prod = vd.get('production', {}) or {}

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    # Prefer the exact capacity the game reported; fall back to the level formula.
    cap = _i(vd.get('storage_max')) or _warehouse_capacity(levels.get('storage'))
    pop_cap = _i(vd.get('pop_max')) or _farm_capacity(levels.get('farm'))

    resources = []
    for key, label in (('wood', 'Wood'), ('stone', 'Clay'), ('iron', 'Iron')):
        stored = _i(res.get(key))
        resources.append({'key': key, 'label': label, 'stored': stored, 'cap': cap,
                          'pct': min(100, round(stored * 100 / cap)) if cap else 0,
                          'full': cap and stored >= cap * 0.97,
                          'prod': _i(prod.get(key))})
    # pop_used is exact when present; otherwise resman stores free pop, so derive used.
    if vd.get('pop_used') is not None:
        pop = _i(vd.get('pop_used'))
    else:
        pop = max(0, pop_cap - _i(res.get('pop')))
    total = {k: _i(v) for k, v in (vd.get('troops', {}) or {}).items()}
    home = {k: _i(v) for k, v in (vd.get('available_troops', {}) or {}).items()}
    away = {k: max(0, total.get(k, 0) - home.get(k, 0)) for k in total}
    return {
        'id': str(vid),
        'name': vd.get('name') or public.get('name') or vid,
        'location': public.get('location'),
        'points': public.get('points'),
        'pop_now': pop, 'pop_cap': pop_cap,
        'pop_pct': min(100, round(pop * 100 / pop_cap)) if pop_cap else 0,
        'resources': resources,
        'storage_level': levels.get('storage'),
        'farm_level': levels.get('farm'),
        'troops_total': total, 'troops_home': home, 'troops_away': away,
        'queue_count': vd.get('active_building_queue', 0),
        'queue_plan': (vd.get('building_queue') or [])[:8],
        'under_attack': vd.get('under_attack'),
        'scavenge_state': vd.get('scavenge_state'),
        'has_snapshot': bool(vd),
    }


@app.route('/village', methods=['GET'])
def get_village_config():
    data = sync()
    vid = request.args.get("id", None)
    return render_template('village.html', data=data, config=pre_process_village_config(village_id=vid),
                           detail=pre_process_village_detail(data, vid),
                           current_select=vid, helpfile=help_file)


@app.route('/map', methods=['GET'])
def get_map():
    sync_data = sync()
    center_id = request.args.get("center", None)
    # No managed villages (e.g. no world cookie -> empty default world): there
    # is nothing to center on, don't crash the whole page.
    center = center_id or next(iter(sync_data['bot']), None)
    map_data = json.dumps(MapBuilder.build(sync_data['villages'], current_village=center, size=15))
    return render_template('map.html', data=sync_data, map=map_data)


def pre_process_overrides(data):
    """Per-village override rows for the villages page.

    A village "overrides" the global village_template when its stored config
    differs from it in any field. Fields that are inherently per-village
    (additional_farms) or that the template itself doesn't carry are ignored.
    """
    config = data.get('config', {}) or {}
    template = config.get('village_template', {}) or {}
    villages_cfg = config.get('villages', {}) or {}
    ignore = {'additional_farms'}
    compare_keys = [k for k in template.keys() if k not in ignore]

    rows = []
    overriding = 0
    for vid, vdata in (data.get('bot', {}) or {}).items():
        vid = str(vid)
        public = (vdata or {}).get('public', {}) or {}
        loc = public.get('location')
        coord = '{}|{}'.format(loc[0], loc[1]) if isinstance(loc, (list, tuple)) and len(loc) == 2 else None
        vcfg = villages_cfg.get(vid)

        if vcfg is None:
            rows.append({
                'id': vid,
                'name': vdata.get('name') or public.get('name') or vid,
                'coord': coord,
                'has_config': False,
                'overrides': False,
                'diff': [],
                'building': None,
                'units': None,
                'managed': False,
                'gather_enabled': bool(template.get('gather_enabled', False)),
                'farm_enabled': bool(template.get('farm_enabled', True)),
            })
            continue

        # Only fields the village explicitly carries count as overrides; a
        # missing key means it inherits the template (e.g. a setting added
        # after the village was saved), not that it diverges from it.
        diff = [k for k in compare_keys if k in vcfg and vcfg.get(k) != template.get(k)]
        is_override = bool(diff)
        if is_override:
            overriding += 1
        rows.append({
            'id': vid,
            'name': vdata.get('name') or public.get('name') or vid,
            'coord': coord,
            'has_config': True,
            'overrides': is_override,
            'diff': diff,
            'building': vcfg.get('building'),
            'units': vcfg.get('units'),
            'managed': bool(vcfg.get('managed')),
            'gather_enabled': bool(vcfg.get('gather_enabled', template.get('gather_enabled', False))),
            'farm_enabled': bool(vcfg.get('farm_enabled', template.get('farm_enabled', True))),
        })

    rows.sort(key=lambda r: (not r['overrides'], r['name'].lower()))
    total = len(rows)
    summary = '{} of {} village{} override the global template.'.format(
        overriding, total, '' if total == 1 else 's')
    return {'rows': rows, 'summary': summary, 'overriding': overriding, 'total': total}


@app.route('/villages', methods=['GET'])
def get_village_overview():
    data = sync()
    return render_template('villages.html', data=data, overrides=pre_process_overrides(data))


@app.route('/building_templates', methods=['GET', 'POST'])
def get_building_templates():
    if request.form.get('new', None):
        plain = os.path.basename(request.form.get('new'))
        if not plain.endswith('.txt'):
            plain = "%s.txt" % plain
        tempfile = '../templates/builder/%s' % plain
        if not os.path.exists(tempfile):
            with open(tempfile, 'w') as ouf:
                ouf.write("")
    selected = request.args.get('t', None)
    return render_template('templates.html',
                           templates=BuildingTemplateManager.template_cache_list(),
                           selected=selected,
                           buildings=buildings)


@app.route('/app/template/building/save', methods=['POST'])
def building_template_save():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not name:
        return jsonify({"ok": False, "error": "missing name"})
    rows = []
    for entry in payload.get("rows", []):
        building = (entry.get("building") or "").strip()
        if building not in buildings:
            continue
        try:
            level = int(entry.get("level"))
        except (TypeError, ValueError):
            continue
        if level < 1:
            continue
        rows.append((building, level))
    BuildingTemplateManager.template_save(name, rows)
    return jsonify({"ok": True, "rows": len(rows)})


@app.route('/app/template/building/delete', methods=['POST'])
def building_template_delete():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name") or request.form.get("name")
    if not name:
        return jsonify({"ok": False, "error": "missing name"})
    return jsonify({"ok": BuildingTemplateManager.template_delete(name)})


@app.route('/unit_templates', methods=['GET', 'POST'])
def get_unit_templates():
    if request.form.get('new', None):
        plain = os.path.basename(request.form.get('new'))
        if not plain.endswith('.txt'):
            plain = "%s.txt" % plain
        path = UnitTemplateManager._template_path(plain)
        if not os.path.exists(path):
            UnitTemplateManager.template_save(plain, [])
    selected = request.args.get('t', None)
    return render_template('unit_templates.html',
                           templates=UnitTemplateManager.template_cache_list(),
                           selected=selected,
                           stages=UnitTemplateManager.template_get(selected) if selected else [],
                           buildings=buildings,
                           units=unit_list)


# Keys a stage owns directly; anything else (e.g. legacy "farm"/"stable") is passed
# through unchanged so editing a hand-written template never loses data.
STAGE_MANAGED_KEYS = ("building", "level", "build", "upgrades", "units", "extra")


def normalize_unit_stage(stage):
    """Turn an editor stage into the on-disk format, grouping units by building."""
    building = (stage.get("building") or "").strip()
    if building not in buildings:
        return None
    try:
        level = int(stage.get("level"))
    except (TypeError, ValueError):
        return None
    if level < 1:
        return None

    out = {"building": building, "level": level}

    upgrades = {}
    for unit, lvl in (stage.get("upgrades") or {}).items():
        if unit not in unit_list:
            continue
        try:
            lvl = int(lvl)
        except (TypeError, ValueError):
            continue
        if lvl >= 1:
            upgrades[unit] = lvl
    if upgrades:
        out["upgrades"] = upgrades

    build = {}
    for unit, amount in (stage.get("units") or {}).items():
        if unit not in unit_list:
            continue
        try:
            amount = int(amount)
        except (TypeError, ValueError):
            continue
        if amount >= 1:
            build.setdefault(unit_building[unit], {})[unit] = amount
    out["build"] = build

    # Preserve any non-managed keys the template author had (farm compositions, etc.).
    for key, value in (stage.get("extra") or {}).items():
        if key not in STAGE_MANAGED_KEYS:
            out[key] = value
    return out


@app.route('/app/template/unit/save', methods=['POST'])
def unit_template_save():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not name:
        return jsonify({"ok": False, "error": "missing name"})
    stages = []
    for raw in payload.get("stages", []):
        stage = normalize_unit_stage(raw)
        if stage is not None:
            stages.append(stage)
    UnitTemplateManager.template_save(name, stages)
    return jsonify({"ok": True, "stages": len(stages)})


@app.route('/app/template/unit/delete', methods=['POST'])
def unit_template_delete():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name") or request.form.get("name")
    if not name:
        return jsonify({"ok": False, "error": "missing name"})
    return jsonify({"ok": UnitTemplateManager.template_delete(name)})


# Quick-toggle whitelist: key -> (label, config path). The first four are global
# booleans; "scavenge" is per-village so it is broadcast to every village.
QUICK_TOGGLES = {
    "farm": ("Farming", "farms.farm"),
    "recruit": ("Recruiting", "units.recruit"),
    "build": ("Building", "building.manage_buildings"),
    "trade": ("Trading", "market.auto_trade"),
    "scavenge": ("Scavenging", "village_template.gather_enabled"),
    "scavenge_attacked": ("Scavenge when attacked", "village_template.gather_when_attacked"),
    "scavenge_night": ("Night consolidate", "village_template.gather_night_consolidate"),
}

# Per-village quick toggles are broadcast to every village (not a global section).
PER_VILLAGE_TOGGLES = {"scavenge": "gather_enabled",
                       "scavenge_attacked": "gather_when_attacked",
                       "scavenge_night": "gather_night_consolidate"}


def quick_settings_state():
    """Current on/off value for each quick toggle, for rendering the side panel."""
    config = DataReader.config_grab()
    state = []
    for key, (label, path) in QUICK_TOGGLES.items():
        section, param = path.split('.')
        on = bool(config.get(section, {}).get(param, False))
        state.append({"key": key, "label": label, "on": on})
    return state


@app.route('/app/quick/set', methods=['GET'])
def quick_set():
    key = request.args.get("key")
    value = request.args.get("value")
    if key not in QUICK_TOGGLES:
        return jsonify({"ok": False, "error": "unknown toggle"})
    if key in PER_VILLAGE_TOGGLES:
        # Per-village setting: apply everywhere so the toggle is account-wide.
        DataReader.broadcast_village_set(PER_VILLAGE_TOGGLES[key], value)
    else:
        DataReader.config_set(parameter=QUICK_TOGGLES[key][1], value=value)
    return jsonify({"ok": True})


# Per-village scavenging parameters that the Farms page broadcasts account-wide.
SCAVENGE_PARAMS = ("gather_enabled", "gather_selection", "advanced_gather",
                   "gather_when_attacked",
                   "gather_night_consolidate", "gather_night_start", "gather_night_end",
                   "gather_exclude_units",
                   "scavenge_unlock_enabled", "prioritize_scavenge_unlock",
                   "scavenge_unlock_hq_1", "scavenge_unlock_hq_2",
                   "scavenge_unlock_hq_3", "scavenge_unlock_hq_4")


@app.route('/app/scavenge/set', methods=['GET'])
def scavenge_set():
    """Broadcast a scavenging setting to every village + the village template."""
    parameter = request.args.get("parameter")
    value = request.args.get("value")
    if parameter not in SCAVENGE_PARAMS:
        return jsonify({"ok": False, "error": "unknown parameter"})
    DataReader.broadcast_village_set(parameter, value)
    return jsonify({"ok": True})


def farm_settings_state():
    """Current farm config + a per-village scavenging snapshot for the Farms page."""
    config = DataReader.config_grab()
    farms = config.get("farms", {}) or {}
    template = config.get("village_template", {}) or {}
    villages = config.get("villages", {}) or {}
    managed = DataReader.cache_grab("villages") or {}

    def village_name(vid):
        info = managed.get(str(vid), {}) or {}
        return info.get("name") or (info.get("public", {}) or {}).get("name") or vid

    per_village = []
    for vid, vcfg in villages.items():
        per_village.append({
            "id": vid,
            "name": village_name(vid),
            "gather_enabled": bool(vcfg.get("gather_enabled", False)),
            "gather_selection": vcfg.get("gather_selection", template.get("gather_selection", 1)),
            "advanced_gather": bool(vcfg.get("advanced_gather", template.get("advanced_gather", True))),
            "gather_when_attacked": bool(vcfg.get("gather_when_attacked", template.get("gather_when_attacked", False))),
            "gather_night_consolidate": bool(vcfg.get("gather_night_consolidate", template.get("gather_night_consolidate", False))),
        })
    per_village.sort(key=lambda v: str(v["name"]))

    # Account-wide scavenging defaults shown on the broadcast controls: fall back to
    # the village template, since that is what new villages inherit.
    scavenge = {
        "gather_enabled": bool(template.get("gather_enabled", False)),
        "gather_selection": template.get("gather_selection", 1),
        "advanced_gather": bool(template.get("advanced_gather", True)),
        # Units NOT to send scavenging (e.g. keep light cav for farming). Stored as
        # an exclude list; the UI shows the inverse ("scavenge with these units").
        "gather_exclude_units": list(template.get("gather_exclude_units", []) or []),
        "gather_when_attacked": bool(template.get("gather_when_attacked", False)),
        # Group policies (alpha): in-game group (name or id) -> never /
        # pause_attacked / always; authoritative over the per-village flag.
        "gather_group_policies": dict(farms.get("gather_group_policies") or {}),
        # The in-game groups the incoming tracker has cached, for the picker.
        "village_groups": DataReader.groups_grab(),
        # Compact picker data: one resolved row per assigned policy + the
        # groups still without one (accounts can have 30+ groups; listing
        # them all as rows would swamp the page).
        "group_policy_rows": None,  # filled below
        "group_policy_unassigned": None,
        "archers_enabled": bool((config.get("world", {}) or {}).get("archers_enabled", False)),
        "gather_night_consolidate": bool(template.get("gather_night_consolidate", False)),
        "gather_night_start": template.get("gather_night_start", 23),
        "gather_night_end": template.get("gather_night_end", 7),
        "scavenge_unlock_enabled": bool(template.get("scavenge_unlock_enabled", False)),
        "prioritize_scavenge_unlock": bool(template.get("prioritize_scavenge_unlock", False)),
        "scavenge_unlock_hq_1": template.get("scavenge_unlock_hq_1", 1),
        "scavenge_unlock_hq_2": template.get("scavenge_unlock_hq_2", 5),
        "scavenge_unlock_hq_3": template.get("scavenge_unlock_hq_3", 8),
        "scavenge_unlock_hq_4": template.get("scavenge_unlock_hq_4", 15),
    }
    # Barb shaper (alpha): config with defaults + the send/result log the
    # game-side BarbShaper keeps in cache/barbshaper.json.
    shaper_entries = []
    try:
        shaper_path = DataReader.data_path("cache", "barbshaper.json")
        if os.path.exists(shaper_path):
            with open(shaper_path) as f:
                for vid, e in (json.load(f) or {}).items():
                    e = dict(e or {})
                    e["id"] = vid
                    e["source_name"] = village_name(e.get("source"))
                    shaper_entries.append(e)
            shaper_entries.sort(key=lambda e: e.get("sent_at", 0), reverse=True)
    except Exception:
        pass
    # Cost preview per wall level, using the exact same math the bot runs.
    costs = []
    try:
        from game.barbshaper import BarbShaper
        tolerance = float(farms.get("shaper_loss_tolerance", 1.0) or 1.0)
        for wall in (3, 5, 7, 10, 12, 15, 20):
            rams = BarbShaper.rams_to_raze(wall)
            axes = BarbShaper.axes_needed(rams, wall, tolerance, 100000)
            costs.append({"wall": wall, "rams": rams, "axes": axes})
    except Exception:
        pass
    shaper = {
        "enabled": bool(farms.get("barb_shaper", False)),
        "min_wall": farms.get("shaper_min_wall", 2),
        "loss_tolerance": farms.get("shaper_loss_tolerance", 1.0),
        "max_sends": farms.get("shaper_max_sends", 2),
        "ram_reserve": farms.get("shaper_ram_reserve", 0),
        "share_axes": bool(farms.get("shaper_share_axes", False)),
        "axe_cap": farms.get("shaper_axe_cap", 0),
        "max_travel_hours": farms.get("shaper_max_travel_hours", 0),
        "entries": shaper_entries,
        "costs": costs,
    }

    # Resolve the group-policy map into display rows (one per assigned
    # policy) + the groups still without one, for the compact picker.
    groups = scavenge["village_groups"] or []
    by_key = {}
    for g in groups:
        by_key[str(g.get("name", "")).lower()] = g
        by_key[str(g.get("id", ""))] = g
    rows = []
    taken = set()
    for key, policy in scavenge["gather_group_policies"].items():
        g = by_key.get(str(key).lower()) or by_key.get(str(key))
        if g:
            taken.add(str(g.get("id")))
        rows.append({
            "key": key, "policy": policy,
            "name": g.get("name") if g else key,
            "type": g.get("type") if g else None,
            "villages": len(g.get("villages") or []) if g else None,
            "missing": g is None,
        })
    rows.sort(key=lambda r: str(r["name"]).lower())
    scavenge["group_policy_rows"] = rows
    scavenge["group_policy_unassigned"] = sorted(
        (g for g in groups if str(g.get("id")) not in taken),
        key=lambda g: str(g.get("name", "")).lower())

    return {"farms": farms, "scavenge": scavenge, "villages": per_village,
            "shaper": shaper}


@app.route('/farms', methods=['GET'])
def get_farms():
    data = sync()
    return render_template('farms.html', data=data, helpfile=help_file,
                           farms=farm_settings_state(),
                           playerfarms=PlayerFarmOverview.build(data))


@app.route('/app/playerfarm/add', methods=['POST'])
def playerfarm_add():
    """Add a target to the player-farm hit list. Expects JSON: target_x,
    target_y, source_id, units {unit: count}, interval_min."""
    body = request.get_json(silent=True) or {}
    entry, error = DataReader.playerfarm_add(
        target_x=body.get("target_x"),
        target_y=body.get("target_y"),
        source_id=body.get("source_id"),
        units=body.get("units") or {},
        interval_min=body.get("interval_min"),
    )
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "entry": entry})


@app.route('/app/playerfarm/estimate', methods=['GET'])
def playerfarm_estimate():
    """Production calculator: ?x=&y=&interval_min= uses the newest scout intel
    for that village; passing wood/stone/iron (mine levels, optional storage)
    computes from those instead."""
    args = request.args
    levels = None
    if any(args.get(k) for k in ("wood", "stone", "iron")):
        levels = {k: args.get(k) or 0 for k in ("wood", "stone", "iron")}
        if args.get("storage"):
            levels["storage"] = args.get("storage")
    estimate, error = DataReader.playerfarm_estimate(
        args.get("x"), args.get("y"), args.get("interval_min"), levels=levels)
    if error:
        return jsonify({"ok": False, "error": error})
    return jsonify({"ok": True, "estimate": estimate})


@app.route('/app/playerfarm/toggle', methods=['GET', 'POST'])
def playerfarm_toggle():
    fid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    state = DataReader.playerfarm_toggle(fid)
    return jsonify({"ok": state is not None, "state": state})


@app.route('/app/playerfarm/remove', methods=['GET', 'POST'])
def playerfarm_remove():
    fid = request.args.get("id") or (request.get_json(silent=True) or {}).get("id")
    return jsonify({"ok": bool(DataReader.playerfarm_remove(fid))})


@app.route('/app/village/apply_template', methods=['POST'])
def village_apply_template():
    """Reset one village (or all, when no id) to the default village_template settings."""
    vid = request.args.get("village_id")
    if vid is None:
        vid = (request.get_json(silent=True) or {}).get("village_id")
    applied = DataReader.apply_village_template(vid)
    if applied is False:
        return jsonify({"ok": False, "error": "no village_template configured"})
    return jsonify({"ok": True, "applied": applied})


@app.route('/app/village/apply_opening', methods=['POST'])
def village_apply_opening():
    """Apply the 'opening (into off)' spear-rush + scavenging preset to one village."""
    vid = request.args.get("village_id")
    if vid is None:
        vid = (request.get_json(silent=True) or {}).get("village_id")
    if not DataReader.apply_opening_strategy(vid):
        return jsonify({"ok": False, "error": "village has no config entry"})
    return jsonify({"ok": True})


@app.route('/app/incoming/tag', methods=['POST'])
def incoming_tag():
    command_id = request.form.get("command_id")
    tag = request.form.get("tag", "")
    if not command_id:
        return jsonify({"ok": False, "error": "missing command_id"})
    saved = DataReader.incoming_tag_set(command_id, tag)
    # Also try to push it to TribalWars as the attack's in-game label. This is
    # best-effort: it only works once the bot has captured the rename endpoint
    # from a logged-in incomings page, and the tag is always saved locally first.
    ingame = DataReader.incoming_rename_ingame(command_id, tag) if tag else {"ok": False, "reason": "empty"}
    return jsonify({"ok": saved, "ingame": ingame})


@app.route('/app/session/set', methods=['POST'])
def session_set():
    raw = request.form.get("session", "")
    return jsonify({"ok": DataReader.session_set(raw)})


@app.route('/app/portal-cookies/set', methods=['POST'])
def portal_cookies_set():
    raw = request.form.get("cookies", "")
    return jsonify({"ok": DataReader.portal_cookies_set(raw)})


@app.route('/app/tw-open', methods=['GET'])
def tw_open():
    from urllib.parse import urlparse
    session = DataReader.get_session()
    endpoint = session.get("endpoint") or ""
    domain = urlparse(endpoint).hostname or ""
    return render_template('tw_open.html', endpoint=endpoint, domain=domain)


@app.route('/app/tw-cookies-export', methods=['GET'])
def tw_cookies_export():
    import time
    from urllib.parse import urlparse
    session = DataReader.get_session()
    game_cookies = session.get("cookies") or {}
    if not game_cookies:
        # No world selected (e.g. extension fetch without the dashboard's
        # world cookie) — fall back to the most recently active world.
        import glob
        candidates = glob.glob(os.path.join(
            DataReader.project_root(), "worlds", "*", "cache", "session.json"))
        if candidates:
            newest = max(candidates, key=os.path.getmtime)
            world_name = os.path.basename(os.path.dirname(os.path.dirname(newest)))
            DataReader.set_active_world(world_name)
            session = DataReader.get_session()
            game_cookies = session.get("cookies") or {}
    endpoint = session.get("endpoint") or ""
    portal_cookies = DataReader.portal_cookies_get()
    game_domain = urlparse(endpoint).hostname or ""
    expiry = int(time.time()) + 60 * 60 * 24 * 30

    def make_entries(cookies, domain):
        return [
            {
                "name": k, "value": v,
                "domain": domain, "hostOnly": True,
                "path": "/", "secure": True, "httpOnly": True,
                "sameSite": "no_restriction", "session": False,
                "expirationDate": expiry,
            }
            for k, v in cookies.items()
        ]

    cookie_list = make_entries(game_cookies, game_domain)
    if portal_cookies:
        cookie_list += make_entries(
            portal_cookies, DataReader.portal_domain(game_domain))

    resp = jsonify(cookie_list)
    resp.headers["Content-Disposition"] = (
        'attachment; filename="tw-cookies-%s.json"' % (game_domain or "export")
    )
    return resp


@app.route('/app/tw-extension.zip', methods=['GET'])
def tw_extension_zip():
    import io
    import zipfile
    ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'browser-extension')
    if not os.path.isdir(ext_dir):
        return "Extension folder not found on server.", 404
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in sorted(os.listdir(ext_dir)):
            fpath = os.path.join(ext_dir, fname)
            if not os.path.isfile(fpath):
                continue
            if fname == "background.js":
                # Bake in this server's address and the selected world so the
                # extension works out of the box, no Options needed.
                with open(fpath) as f:
                    code = f.read()
                code = code.replace(
                    'const DEFAULT_SERVER = "http://localhost:5000";',
                    'const DEFAULT_SERVER = %s;' % json.dumps(request.host_url.rstrip('/')))
                code = code.replace(
                    'const DEFAULT_WORLD = "";',
                    'const DEFAULT_WORLD = %s;' % json.dumps(DataReader.active_world() or ""))
                zf.writestr(fname, code)
            elif fname == "manifest.json":
                # Point the content script at this server so the dashboard's
                # "Open game" button can talk to the extension, and put the
                # world in the name so multiple copies are distinguishable.
                with open(fpath) as f:
                    manifest = json.load(f)
                match = request.host_url.rstrip('/') + "/*"
                for cs in manifest.get("content_scripts", []):
                    if match not in cs["matches"]:
                        cs["matches"].append(match)
                world = DataReader.active_world()
                if world:
                    manifest["name"] += " (%s)" % world
                    manifest["action"]["default_title"] = \
                        "Open TribalWars %s with bot session" % world
                zf.writestr(fname, json.dumps(manifest, indent=2))
            else:
                zf.write(fpath, fname)
    buf.seek(0)
    zip_name = "twb-session-extension-%s.zip" % (DataReader.active_world() or "default")
    return Response(buf.read(), content_type='application/zip',
                    headers={'Content-Disposition': 'attachment; filename="%s"' % zip_name})


@app.route('/app/tw-proxy', methods=['GET'])
@app.route('/app/tw-proxy/<path:subpath>', methods=['GET'])
def tw_proxy(subpath=''):
    import requests as _req
    import re
    from urllib.parse import urlparse

    session = DataReader.get_session()
    cookies = session.get("cookies") or {}
    endpoint = session.get("endpoint") or ""
    if not endpoint or endpoint == "None":
        return "No game endpoint configured — run the bot first.", 503

    parsed = urlparse(endpoint)
    base = "%s://%s" % (parsed.scheme, parsed.netloc)

    if subpath:
        url = base + "/" + subpath
        qs = request.query_string.decode()
        if qs:
            url += "?" + qs
    else:
        url = endpoint

    config = DataReader.config_grab()
    ua = (config.get("bot") or {}).get("user_agent") or "Mozilla/5.0"

    try:
        tw_resp = _req.get(url, cookies=cookies,
                           headers={"User-Agent": ua,
                                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                                    "Accept-Language": "nl,en;q=0.5"},
                           allow_redirects=True, timeout=20)
    except Exception as e:
        return "Proxy request failed: %s" % str(e), 502

    # If TW redirected us off the game domain the session is invalid
    final_host = urlparse(tw_resp.url).hostname or ""
    if final_host != parsed.hostname:
        return render_template("tw_proxy_dead.html", redirect_url=tw_resp.url,
                               domain=parsed.hostname), 401

    ct = tw_resp.headers.get("Content-Type", "text/html")
    if "text/html" not in ct:
        return Response(tw_resp.content, content_type=ct)

    html = tw_resp.text

    # Strip ALL JavaScript — TW's JS detects the wrong hostname and redirects.
    # The overview HTML is fully server-rendered so this gives a clean read-only snapshot.
    html = re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<noscript\b[^>]*>.*?</noscript>', '', html, flags=re.DOTALL | re.IGNORECASE)

    # Point assets (CSS, images) directly at TW so they load without auth
    base_tag = '<base href="%s/">' % base

    banner = (
        '<div style="position:fixed;top:0;left:0;right:0;z-index:99999;'
        'background:#E07B2C;color:#fff;padding:7px 16px;font:600 13px/1 sans-serif;'
        'display:flex;align-items:center;gap:12px;box-shadow:0 2px 8px rgba(0,0,0,.5)">'
        '<span>&#128065; TWB read-only snapshot &mdash; JS disabled, navigation limited</span>'
        '<span style="margin-left:auto;display:flex;gap:12px">'
        '<a href="/app/tw-proxy" style="color:#fff;text-decoration:underline">Refresh</a>'
        '<a href="/" style="color:#fff;text-decoration:underline">Dashboard</a>'
        '</span></div><div style="padding-top:38px">'
    )

    html_lower = html.lower()
    head_idx = html_lower.find("<head>")
    if head_idx != -1:
        insert_at = head_idx + len("<head>")
        html = html[:insert_at] + "\n" + base_tag + "\n" + html[insert_at:]

    body_idx = html_lower.find("<body")
    if body_idx != -1:
        end = html.index(">", body_idx) + 1
        html = html[:end] + "\n" + banner + html[end:]
        html = html.replace("</body>", "</div></body>", 1)

    return Response(html, content_type="text/html; charset=utf-8")


@app.route('/', methods=['GET'])
def get_home():
    session = DataReader.get_session()
    data = sync()
    return render_template('bot.html', data=data, session=session,
                           overview=OverviewBuilder.build(data),
                           quick=quick_settings_state(),
                           portal_domain=DataReader.portal_domain(),
                           portal_saved=bool(DataReader.portal_cookies_get()))


def _bot_log_path(world):
    """Return the bot's live rotating log (cache/twb.log) for a world.

    This is the file setup_file_logging() writes via Python logging, so it stays
    current no matter how the bot was launched - tmux, start.sh, or the dashboard's
    own BotManager.start(). The old bot_<world>.log only captured stdout from a
    dashboard-initiated start, so it went stale (showing a days-old "last report")
    whenever the bot was started another way.
    """
    wd = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    key = os.path.basename(str(world).strip()) if world and str(world).strip() else ""
    if key:
        return os.path.join(wd, "worlds", key, "cache", "twb.log")
    return os.path.join(wd, "cache", "twb.log")


@app.route('/logs', methods=['GET'])
def get_logs():
    world = DataReader.active_world()
    return render_template('logs.html', data=sync(), world=world or 'default',
                           log_path=_bot_log_path(world))


@app.route('/bot/log', methods=['GET'])
def get_bot_log():
    """Return the last N lines of the bot's log file as JSON."""
    world = DataReader.active_world()
    path = _bot_log_path(world)
    n = min(int(request.args.get('n', 200)), 500)
    if not os.path.exists(path):
        return jsonify({'lines': [], 'missing': True, 'path': path})
    try:
        with open(path, 'r', errors='replace') as fh:
            lines = list(collections.deque(fh, maxlen=n))
        return jsonify({'lines': [l.rstrip('\n') for l in lines], 'missing': False, 'path': path})
    except Exception as e:
        return jsonify({'lines': [], 'missing': True, 'error': str(e), 'path': path})


@app.route('/app/config/set', methods=['GET'])
def config_set():
    vid = request.args.get("village_id", None)
    if not vid:
        DataReader.config_set(parameter=request.args.get("parameter"), value=request.args.get("value", None))
    else:
        param = request.args.get("parameter")
        if param.startswith("village."):
            param = param.replace("village.", "")
        DataReader.village_config_set(village_id=vid, parameter=param, value=request.args.get("value", None))

    return jsonify(sync())


def _is_local_host(host):
    return host in ("localhost", "127.0.0.1", "::1")


if len(sys.argv) > 1:
    # Pass a second argument to bind on all interfaces, e.g.:
    #   python server.py 5000 0.0.0.0
    # so the dashboard becomes reachable from other devices on your network.
    # Leave it off (host stays localhost) to keep it local-only.
    host = sys.argv[2] if len(sys.argv) > 2 else "localhost"
    debug = _is_local_host(host)
    if not debug:
        print(
            "WARNING: dashboard bound to %s with no authentication - the "
            "interactive debugger is disabled, but do not expose this panel to "
            "untrusted networks." % host
        )
    app.run(host=host, port=sys.argv[1], debug=debug)
else:
    # Default no-arg run binds localhost only, so the debugger is safe here.
    app.run(debug=True)
