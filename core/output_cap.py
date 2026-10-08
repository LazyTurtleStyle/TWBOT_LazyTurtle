"""Keeps a bot's captured console output from growing without limit.

In a terminal or a tmux pane the console scrolls away. Anything else that
starts the bot captures it instead: the dashboard's Start button appends it to
worlds/<world>/bot_<world>.log, start.sh without tmux to cache/logs/bot-*.out.
Neither file is ever rotated, the console logs every request, and a bot is
meant to run for months - measured on a live world that is 7 to 13 MB a day.

Whoever opened the file has long since let go of it, so the bot minds its own
output. Nothing is lost that matters: cache/twb.log is the log that is kept,
rotated and shown on the dashboard. This file is only the raw console.
"""
import logging
import os
import stat
import sys

# Emptied once it passes this. Days of console at the measured rate.
MAX_BYTES = 10_000_000


def cap_redirected_output(max_bytes=MAX_BYTES, streams=None):
    """Empty stdout/stderr if they are a plain file that has grown too big.

    A terminal, a pipe (Docker) and a socket (systemd's journal) are left
    alone - each of those has its own keeper. Returns the bytes freed.
    """
    freed = 0
    seen = set()
    for stream in streams or (sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
            info = os.fstat(fd)
        except (AttributeError, OSError, ValueError):
            continue
        # stdout and stderr are normally the same file; empty it once.
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) in seen:
            continue
        seen.add((info.st_dev, info.st_ino))
        if info.st_size <= max_bytes:
            continue
        try:
            stream.flush()
            os.ftruncate(fd, 0)
            # A file opened for appending writes at the new end by itself.
            # One opened with a plain ">" - and any file on Windows - keeps its
            # old position, and the next line would land 10 MB into an empty
            # file with the gap filled in again.
            os.lseek(fd, 0, os.SEEK_SET)
        except OSError:
            continue
        freed += info.st_size
    if freed:
        logging.getLogger("twb").info(
            "Console output file passed %d MB and was emptied. The history is "
            "in cache/twb.log.", max_bytes // 1_000_000)
    return freed
