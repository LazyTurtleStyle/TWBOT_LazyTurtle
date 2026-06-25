import json
import os
import sys
sys.path.insert(0, "../")

from flask import Flask, jsonify, send_from_directory, request, render_template

try:
    from webmanager.helpfile import (help_file, buildings, section_labels, config_groups,
                                     section_setup, unit_building, unit_list)
    from webmanager.utils import (DataReader, BotManager, MapBuilder, BuildingTemplateManager,
                                  UnitTemplateManager, OverviewBuilder)
except ImportError:
    from helpfile import (help_file, buildings, section_labels, config_groups,
                          section_setup, unit_building, unit_list)
    from utils import (DataReader, BotManager, MapBuilder, BuildingTemplateManager,
                       UnitTemplateManager, OverviewBuilder)

import datetime
from html import escape as html_escape

bm = BotManager()

app = Flask(__name__)
app.config["DEBUG"] = True


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


def control_for(key, value, village_id=None):
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


def pre_process_config():
    config = sync()['config']
    to_hide = ["build", "villages"]
    sections = {}
    for section in config:
        if section in to_hide or not isinstance(config[section], dict):
            continue
        sections[section] = render_grouped(section, section, config[section])
    return sections


def pre_process_village_config(village_id):
    config = sync()['config']['villages']
    if village_id in config:
        config = config[village_id]
    else:
        config = config[list(config.keys())[0]]
    return render_grouped('village_template', 'village', config, village_id)


def sync():
    reports = DataReader.cache_grab("reports")
    villages = DataReader.cache_grab("villages")
    attacks = DataReader.cache_grab("attacks")
    config = DataReader.config_grab()
    managed = DataReader.cache_grab("managed")
    bot_status = bm.is_running()

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
    bm.start()
    return jsonify(bm.is_running())


@app.route('/bot/stop')
def stop_bot():
    bm.stop()
    return jsonify(not bm.is_running())


@app.route('/config', methods=['GET'])
def get_config():
    return render_template('config.html', data=sync(), config=pre_process_config(),
                           helpfile=help_file, section_labels=section_labels,
                           section_setup=section_setup)


@app.route('/app/notification/test', methods=['POST'])
def notification_test():
    """Send a test Telegram message using the currently saved config."""
    try:
        from core.notification import Notification
        ok, err = Notification.test()
    except Exception as exc:  # e.g. telegram lib missing
        ok, err = False, str(exc)
    return jsonify({"ok": ok, "error": err})


@app.route('/village', methods=['GET'])
def get_village_config():
    data = sync()
    vid = request.args.get("id", None)
    return render_template('village.html', data=data, config=pre_process_village_config(village_id=vid),
                           current_select=vid, helpfile=help_file)


@app.route('/map', methods=['GET'])
def get_map():
    sync_data = sync()
    center_id = request.args.get("center", None)
    center = next(iter(sync_data['bot'])) if not center_id else center_id
    map_data = json.dumps(MapBuilder.build(sync_data['villages'], current_village=center, size=15))
    return render_template('map.html', data=sync_data, map=map_data)


@app.route('/villages', methods=['GET'])
def get_village_overview():
    return render_template('villages.html', data=sync())


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
}


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
    if key == "scavenge":
        # Per-village setting: apply everywhere so the toggle is account-wide.
        DataReader.broadcast_village_set("gather_enabled", value)
    else:
        DataReader.config_set(parameter=QUICK_TOGGLES[key][1], value=value)
    return jsonify({"ok": True})


# Per-village scavenging parameters that the Farms page broadcasts account-wide.
SCAVENGE_PARAMS = ("gather_enabled", "gather_selection", "advanced_gather")


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
        })
    per_village.sort(key=lambda v: str(v["name"]))

    # Account-wide scavenging defaults shown on the broadcast controls: fall back to
    # the village template, since that is what new villages inherit.
    scavenge = {
        "gather_enabled": bool(template.get("gather_enabled", False)),
        "gather_selection": template.get("gather_selection", 1),
        "advanced_gather": bool(template.get("advanced_gather", True)),
    }
    return {"farms": farms, "scavenge": scavenge, "villages": per_village}


@app.route('/farms', methods=['GET'])
def get_farms():
    return render_template('farms.html', data=sync(), helpfile=help_file,
                           farms=farm_settings_state())


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


@app.route('/', methods=['GET'])
def get_home():
    session = DataReader.get_session()
    data = sync()
    return render_template('bot.html', data=data, session=session,
                           overview=OverviewBuilder.build(data),
                           quick=quick_settings_state())


@app.route('/app/js', methods=['GET'])
def get_js():
    urlpath = os.path.join(os.path.dirname(__file__), "public")
    return send_from_directory(urlpath, "js.v2.js")


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


if len(sys.argv) > 1:
    # Pass a second argument to bind on all interfaces, e.g.:
    #   python server.py 5000 0.0.0.0
    # so the dashboard becomes reachable from other devices on your network.
    # Leave it off (host stays localhost) to keep it local-only.
    host = sys.argv[2] if len(sys.argv) > 2 else "localhost"
    app.run(host=host, port=sys.argv[1])
else:
    app.run()
