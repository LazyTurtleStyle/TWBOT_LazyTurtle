"""Plain-assert checks for core/output_cap.py: python3 tests/test_output_cap.py"""
import io
import logging
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from core.output_cap import cap_redirected_output  # noqa: E402

logging.disable(logging.CRITICAL)
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
LINE = "2026-01-01 00:00:00 - Requests - DEBUG - GET game.php?screen=overview [200]\n"


def path():
    fd, name = tempfile.mkstemp(prefix="twb-output-cap-")
    os.close(fd)
    return name


def test_big_appended_file_is_emptied_and_keeps_working():
    name = path()
    with open(name, "a") as out:
        out.write(LINE * 2000)
        assert cap_redirected_output(max_bytes=50_000, streams=[out]) == len(LINE) * 2000
        out.write(LINE)
    assert open(name).read() == LINE


def test_a_file_not_opened_for_appending_gets_no_hole():
    # What ">" does, and what every file is on Windows: the old position stays.
    name = path()
    with open(name, "w") as out:
        out.write(LINE * 2000)
        cap_redirected_output(max_bytes=50_000, streams=[out])
        out.write(LINE)
    assert open(name, "rb").read() == LINE.encode()


def test_a_small_file_is_left_alone():
    name = path()
    with open(name, "a") as out:
        out.write(LINE * 10)
        assert cap_redirected_output(max_bytes=50_000, streams=[out]) == 0
    assert open(name).read() == LINE * 10


def test_stdout_and_stderr_on_one_file_count_once():
    name = path()
    with open(name, "a") as out, open(name, "a") as err:
        out.write(LINE * 2000)
        out.flush()
        assert cap_redirected_output(max_bytes=50_000, streams=[out, err]) == len(LINE) * 2000


def test_pipes_and_non_files_are_left_alone():
    read_end, write_end = os.pipe()
    with os.fdopen(write_end, "w") as out, os.fdopen(read_end) as back:
        out.write(LINE * 100)
        out.flush()
        assert cap_redirected_output(max_bytes=10, streams=[out, io.StringIO(), None]) == 0
        os.set_blocking(back.fileno(), False)
        assert back.read() == LINE * 100


def test_a_bot_started_the_way_the_dashboard_starts_one():
    # webmanager/utils.py, BotManager.start: both streams appended to one file
    # by a process that then lets go of it.
    name = path()
    child = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core.output_cap import cap_redirected_output\n"
        "line = %r\n"
        "for _ in range(3):\n"
        "    sys.stdout.write(line * 1000); sys.stderr.write(line * 1000)\n"
        "    cap_redirected_output(max_bytes=100_000)\n"
        "print('last line')\n" % (ROOT, LINE))
    with open(name, "a") as log:
        subprocess.run([sys.executable, "-c", child], stdout=log, stderr=log, check=True)
    left = open(name, "rb").read()
    assert b"\x00" not in left and left.endswith(b"last line\n")
    assert len(left) < 100_000 + 2 * 1000 * len(LINE)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
