"""Mass scavenging is the only scavenging while it is on:
python3 tests/test_mass_scavenge_exclusive.py"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from game.village import Village  # noqa: E402


def village(vid, mass):
    v = Village.__new__(Village)
    v.village_id = vid
    v.get_config = lambda section=None, parameter=None, default=None: (
        mass if (section, parameter) == ("farms", "mass_scavenge") else default)
    return v


def test_a_village_outside_the_group_does_not_scavenge_by_itself():
    on = {"enabled": True, "group": "ROOF"}
    # Whatever the group holds: on means the per-village path stands down.
    assert village("1", on)._mass_scavenged() is True
    assert village("999", on)._mass_scavenged() is True


def test_with_mass_scavenging_off_every_village_scavenges_itself():
    assert village("1", {"enabled": False, "group": "ROOF"})._mass_scavenged() is False
    assert village("1", {})._mass_scavenged() is False


if __name__ == "__main__":
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    for name, fn in tests:
        fn()
        print("ok", name)
    print("%d passed" % len(tests))
