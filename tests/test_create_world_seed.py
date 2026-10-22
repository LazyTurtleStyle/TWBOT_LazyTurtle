"""New worlds must seed from the user's tuned root config.json, not the example:
python3 tests/test_create_world_seed.py"""
import collections
import json
import os
import shutil
import sys
import tempfile
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# webmanager.utils pulls dashboard-only deps the test env may lack; the
# create_world path under test touches none of them, so stub them.
for name in ("psutil",):
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)

root = tempfile.mkdtemp()
os.mkdir(os.path.join(root, "webmanager"))
shutil.copy(os.path.join(os.path.dirname(__file__), "..", "webmanager", "utils.py"),
            os.path.join(root, "webmanager", "utils.py"))
with open(os.path.join(root, "webmanager", "__init__.py"), "w") as f:
    f.write("")
sys.path.insert(0, root)

example = {"build": {"version": "2.5"}, "server": {"server": "", "endpoint": ""},
           "bot": {"user_agent": "ua", "delay_factor": 1.0},
           "village_template": {"scout_first": False}, "villages": {}}
tuned = json.loads(json.dumps(example))
tuned["bot"]["delay_factor"] = 1.6
tuned["village_template"]["scout_first"] = True

with open(os.path.join(root, "config.example.json"), "w") as f:
    json.dump(example, f)


def fresh_utils():
    import importlib
    for mod in ("webmanager", "webmanager.utils"):
        sys.modules.pop(mod, None)
    return importlib.import_module("webmanager.utils")


# 1) No root config.json -> falls back to the example
DataReader = fresh_utils().DataReader
r = DataReader.create_world("https://nl99.tribalwars.nl/game.php?screen=overview",
                            user_agent="Mozilla/5.0 test agent")
assert r["ok"], r
cfg = json.load(open(os.path.join(root, "worlds", "nl99", "config.json")))
assert cfg["bot"]["delay_factor"] == 1.0, cfg["bot"]
assert cfg["server"]["server"] == "nl99"

# 2) Root config.json present -> its tuned values win
with open(os.path.join(root, "config.json"), "w") as f:
    json.dump(tuned, f)
r = DataReader.create_world("https://uk91.tribalwars.co.uk/game.php?screen=overview",
                            user_agent="Mozilla/5.0 test agent")
assert r["ok"], r
cfg = json.load(open(os.path.join(root, "worlds", "uk91", "config.json")))
assert cfg["bot"]["delay_factor"] == 1.6, cfg["bot"]
assert cfg["village_template"]["scout_first"] is True
assert cfg["server"]["endpoint"] == "https://uk91.tribalwars.co.uk/game.php"
assert cfg["bot"]["user_agent"] == "Mozilla/5.0 test agent"

shutil.rmtree(root)
print("ok: new worlds seed from root config.json when present, example otherwise")
