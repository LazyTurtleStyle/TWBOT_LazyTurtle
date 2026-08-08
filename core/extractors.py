"""
File used for data extraction
"""

import json
import re


# Precompiled regexes (patterns copied verbatim from the inline versions).
# Python's re module already caches compiled literals, so the win is small, but
# compiling once up front is cleaner and avoids the per-call cache lookup - it
# matters most for the patterns used inside the units_overview row loop.
_RE_VILLAGE_DATA = re.compile(r'var village = (.+);')
_RE_GAME_STATE = re.compile(r'TribalWars\.updateGameData\((.+?)\);')
_RE_BUILDING_DATA = re.compile(r'(?s)BuildingMain.buildings = (\{.+?\});')
_RE_QUESTS = re.compile(r'Quests.setQuestData\((\{.+?\})\);')
_RE_QUEST_REWARDS = re.compile(r'RewardSystem\.setRewards\(\s*(\[\{.+?\}\]),')
_RE_MAP_DATA = re.compile(r'(?s)TWMap.sectorPrefech = (\[(.+?)\]);')
_RE_SMITH_DATA = re.compile(r'(?s)BuildingSmith.techs = (\{.+?\});')
_RE_PREMIUM_DATA = re.compile(r'(?s)PremiumExchange.receiveData\((.+?)\);')
_RE_RECRUIT_DATA = re.compile(r'(?s)unit_managers.units = (\{.+?\});')
_RE_QUOTE_KEYS = re.compile(r'([\{\s,])(\w+)(:)')
_RE_UNITS_HOME = re.compile(r'<table id="units_home".*?</tr>(.*?)</tr>', re.DOTALL)
_RE_UNIT_ITEMS_HOME = re.compile(r'class=\'unit-item unit-item-(.*?)\'[^>]*>(\d+)</td>')
_RE_TOOLTIP = re.compile(r'\s*tooltip\s*')
_RE_BUILD_QUEUE = re.compile('(?s)<table id="build_queue"(.+?)</table>')
_RE_UNITS_TABLE = re.compile(r'(?s)<table id="units_table".*?</table>')
_RE_UNIT_HEADER = re.compile(r'unit-item-(\w+)|unit_(\w+)\b')
_RE_TR = re.compile(r'(?s)<tr.*?</tr>')
_RE_TD = re.compile(r'(?s)<td[^>]*>(.*?)</td>')
_RE_TAG = re.compile(r'<[^>]+>')
_RE_COORD = re.compile(r'\(\d+\|\d+\)')
_RE_NONDIGIT = re.compile(r'\D')
_RE_RECRUIT_QUEUE = re.compile(r'(?s)TrainOverview\.cancelOrder\((\d+)\)')
_RE_VILLAGE_IDS = re.compile(r'<span class="quickedit-vn" data-id="(\w+)"')
_RE_VILLAGE_ANCHOR = re.compile(r'(?s)<span class="village_anchor.+?</tr>')
_RE_UNITS_TOTAL = re.compile(r'(?s)class=\Wunit-item unit-item-([a-z]+)\W.+?(\d+)</td>')
_RE_PLACE_ENTRY_ALL = re.compile(r'units_entry_all_(\w+)[^>]*>\s*\(([\d.,]+)\)')
_RE_ATTACK_FORM = re.compile(r'(?s)<input.+?name="(.+?)".+?value="(.*?)"')
_RE_ATTACK_DURATION = re.compile(r'<span class="relative_time" data-duration="(\d+)"')
_RE_REPORT_TABLE = re.compile(r'(?s)class="report-link" data-id="(\d+)"')
_RE_REPORT_GROUP_SELECT = re.compile(r'(?s)<select name="group_id".+?</select>')
_RE_OPTION_VALUE = re.compile(r'<option value="(\d+)"')
_RE_FARM_ICON = re.compile(
    r'<a([^>]*class="farm_village_(\d+) farm_icon farm_icon_([a-d])[^"]*"[^>]*)>')
_RE_FORECAST = re.compile(r'data-units-forecast="([^"]*)"')


class Extractor:
    """
    Defines various regexes for data retrieval (precompiled at module level).
    """
    @staticmethod
    def village_data(res):
        """
        Detects village data on a page
        """
        if type(res) != str:
            res = res.text
        grabber = _RE_VILLAGE_DATA.search(res)
        if grabber:
            data = grabber.group(1)
            return json.loads(data, strict=False)

    @staticmethod
    def game_state(res):
        """
        Detects the game state that is available on most pages
        """
        if type(res) != str:
            res = res.text
        grabber = _RE_GAME_STATE.search(res)
        if grabber:
            data = grabber.group(1)
            return json.loads(data, strict=False)

    @staticmethod
    def building_data(res):
        """
        Fetches building data from the main building
        """
        if type(res) != str:
            res = res.text
        dre = _RE_BUILDING_DATA.search(res)
        if dre:
            return json.loads(dre.group(1), strict=False)

        return None

    @staticmethod
    def get_quests(res, skip=()):
        """
        Gets quest data on almost any page. ``skip`` holds quest ids the caller
        already tried to complete, so a quest the game keeps reporting as
        finished cannot be handed back over and over.
        """
        if res is None:
            return None
        if type(res) != str:
            res = res.text
        get_quests = _RE_QUESTS.search(res)
        if get_quests:
            result = json.loads(get_quests.group(1), strict=False)
            for quest in result:
                if quest in skip:
                    continue
                data = result[quest]
                if data['goals_completed'] == data['goals_total']:
                    return quest
        return None

    @staticmethod
    def get_quest_rewards(res):
        """
        Detects if there are rewards available for quests
        """
        if type(res) != str:
            res = res.text
        get_rewards = _RE_QUEST_REWARDS.search(res)
        rewards = []
        if get_rewards:
            result = json.loads(get_rewards.group(1), strict=False)
            for reward in result:
                if reward['status'] == "unlocked":
                    rewards.append(reward)
        # Return all off them
        return rewards

    @staticmethod
    def map_data(res):
        """
        Detects other villages on the map page
        """
        if type(res) != str:
            res = res.text
        data = _RE_MAP_DATA.search(res)
        if data:
            result = json.loads(data.group(1), strict=False)
            return result

    @staticmethod
    def smith_data(res):
        """
        Gets smith data
        """
        if type(res) != str:
            res = res.text
        data = _RE_SMITH_DATA.search(res)
        if data:
            result = json.loads(data.group(1), strict=False)
            return result
        return None

    @staticmethod
    def premium_data(res):
        """
        Detects data on the premium exchange page
        """
        if type(res) != str:
            res = res.text
        data = _RE_PREMIUM_DATA.search(res)
        if data:
            result = json.loads(data.group(1), strict=False)
            return result
        return None

    @staticmethod
    def recruit_data(res):
        """
        Fetches recruit data for the current building
        """
        if type(res) != str:
            res = res.text
        data = _RE_RECRUIT_DATA.search(res)
        if data:
            raw = data.group(1)
            processed = _RE_QUOTE_KEYS.sub(r'\1"\2"\3', raw)
            result = json.loads(processed, strict=False)
            return result

    @staticmethod
    def units_in_village(res):
        """
        Detects all units in the village
        """
        if type(res) != str:
            res = res.text
        matches = _RE_UNITS_HOME.search(res)
        # We get the start of the table and grab the 2nd row (Where "From this village" troops are located)
        if matches:
            table_content = matches.group(1)
            unit_matches = _RE_UNIT_ITEMS_HOME.findall(table_content)
            # Find all the tuples (name, quantity) under the class "unit-item unit-item-*troop_name*"
            units = [(_RE_TOOLTIP.sub('', unit_name), unit_quantity) for unit_name, unit_quantity in
                     unit_matches if int(unit_quantity) > 0]
            # Filter units with quantity = 0, also for the Paladin,
            # the name would be "knight tooltip", so we had to remove that.
            return units
        return []

    @staticmethod
    def units_in_place(res):
        """
        Available units on a rally point send form (screen=place): the (N)
        "select all" links next to each unit input. Returns {unit: int}.
        """
        if type(res) != str:
            res = res.text
        return {
            unit: int(count.replace(".", "").replace(",", ""))
            for unit, count in _RE_PLACE_ENTRY_ALL.findall(res)
        }

    @staticmethod
    def active_building_queue(res):
        """
        Detects queued building entries
        """
        if type(res) != str:
            res = res.text
        builder = _RE_BUILD_QUEUE.search(res)
        if not builder:
            return 0

        return builder.group(1).count('<a class="btn btn-cancel"')

    @staticmethod
    def units_overview(res):
        """
        Sum the per-unit troop counts across every village row of an
        overview_villages?mode=units table (e.g. type=moving "op pad", or
        type=away). Returns {unit: count}. Unit column order is read from the
        table header so worlds without archers/paladins parse correctly.
        """
        if type(res) != str:
            res = res.text
        m = _RE_UNITS_TABLE.search(res)
        if not m:
            return {}
        table = m.group(0)
        units = []
        for a, b in _RE_UNIT_HEADER.findall(table):
            unit = a or b
            if unit not in units:
                units.append(unit)
        if not units:
            return {}
        out = {u: 0 for u in units}
        # Each data row: <td>village (x|y)</td><td>status</td> + one <td> per unit.
        for row in _RE_TR.findall(table):
            cells = _RE_TD.findall(row)
            if len(cells) < 2 + len(units):
                continue
            texts = [_RE_TAG.sub('', c).strip() for c in cells]
            if not _RE_COORD.search(texts[0] or ''):
                continue
            for i, unit in enumerate(units):
                digits = _RE_NONDIGIT.sub('', texts[2 + i] or '')
                if digits:
                    out[unit] += int(digits)
        return {u: c for u, c in out.items() if c}

    @staticmethod
    def active_recruit_queue(res):
        """
        Detects active recruitment entries
        """
        if type(res) != str:
            res = res.text
        builder = _RE_RECRUIT_QUEUE.findall(res)
        return builder

    @staticmethod
    def village_ids_from_overview(res):
        """
        Fetches villages from the overview page
        """
        if type(res) != str:
            res = res.text
        villages = _RE_VILLAGE_IDS.findall(res)
        return list(set(villages))

    @staticmethod
    def units_in_total(res):
        """
        Gets total amount of units in a village
        """
        if type(res) != str:
            res = res.text
        # hide units from other villages
        res = _RE_VILLAGE_ANCHOR.sub('', res)
        data = _RE_UNITS_TOTAL.findall(res)
        return data

    @staticmethod
    def attack_form(res):
        """
        Detects input fiels in the attack form
        ... because there are many :)
        """
        if type(res) != str:
            res = res.text
        data = _RE_ATTACK_FORM.findall(res)
        return data

    @staticmethod
    def attack_duration(res):
        """
        Detects the duration of an attack
        """
        if type(res) != str:
            res = res.text
        data = _RE_ATTACK_DURATION.search(res)
        if data:
            return int(data.group(1))
        return 0

    @staticmethod
    def report_table(res):
        """
        Fetches information from a report
        """
        if type(res) != str:
            res = res.text
        data = _RE_REPORT_TABLE.findall(res)
        return data

    @staticmethod
    def report_groups(res):
        """
        Report-folder ids from the group selector on the report screen.
        The main folder (group 0) is implicit and not listed as an option.
        """
        if type(res) != str:
            res = res.text
        select = _RE_REPORT_GROUP_SELECT.search(res)
        if not select:
            return []
        return _RE_OPTION_VALUE.findall(select.group(0))

    @staticmethod
    def farm_assistant_icons(res):
        """
        Reads the per-target A/B/C/D Farm Assistant icons from the am_farm overview page.
        For each village id, returns whether each icon is disabled (greyed out in-game,
        e.g. not enough troops or the wall makes it a guaranteed loss) and, for the C
        ("from report") icon, the exact troop forecast the game itself calculated
        (data-units-forecast), since that troop count isn't something we choose ourselves.
        Returns {village_id: {"a": {...}, "b": {...}, "c": {...}}}, only for icons present.
        """
        if type(res) != str:
            res = res.text
        result = {}
        for match in _RE_FARM_ICON.finditer(res):
            attrs, vid, kind = match.group(1), match.group(2), match.group(3)
            disabled = "farm_icon_disabled" in attrs
            forecast = None
            forecast_match = _RE_FORECAST.search(attrs)
            if forecast_match:
                raw = forecast_match.group(1).replace("&quot;", '"')
                forecast = json.loads(raw, strict=False)
            result.setdefault(vid, {})[kind] = {
                "disabled": disabled,
                "forecast": forecast,
            }
        return result

    @staticmethod
    def daily_bonus_data(res):
        """
        The DailyBonus.init cycle state from the daily-bonus screen: chests
        keyed by day (is_locked / is_collected / reward) plus
        reward_count_unlocked. None when the page carries no daily bonus.
        The init call has several arguments, so the first JSON object is
        parsed with raw_decode instead of a regex.
        """
        if type(res) != str:
            res = res.text
        marker = res.find("DailyBonus.init(")
        if marker == -1:
            return None
        start = res.find("{", marker)
        if start == -1:
            return None
        try:
            data, _ = json.JSONDecoder().raw_decode(res[start:])
            return data
        except ValueError:
            return None
