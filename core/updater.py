"""
Update checking logic
"""

import json
import os.path
import time
import requests
import logging

# Update checking is disabled on purpose: this is a heavily modified fork and we
# don't want it phoning the upstream repository or warning about "new versions"
# that would overwrite local changes. To re-enable against your OWN repository,
# set UPDATE_ENABLED = True and point UPDATE_SOURCE_URL at your raw
# config.example.json.
UPDATE_ENABLED = False
UPDATE_SOURCE_URL = (
    "https://raw.githubusercontent.com/stefan2200/TWB/master/config.example.json"
)


def check_update():
    """
    If enabled, check whether the config template version matches the one on github
    Notify and 5 seconds sleep if update is available
    """
    if not UPDATE_ENABLED:
        return
    get_local_config_template_version = os.path.join(
        os.path.dirname(__file__),
        "..",
        "config.example.json"
    )

    get_local_config_version = os.path.join(
        os.path.dirname(__file__),
        "..",
        "config.json"
    )
    if os.path.exists(get_local_config_version):
        with open(get_local_config_version, "r", encoding="utf-8") as running_cf:
            parsed = json.load(fp=running_cf)
            if not parsed["bot"].get("check_update", False):
                return
    with open(get_local_config_template_version, "r", encoding="utf-8") as local_cf:
        parsed = json.load(fp=local_cf)
        get_remote_version = requests.get(UPDATE_SOURCE_URL).json()
        if parsed["build"]["version"] != get_remote_version["build"]["version"]:
            logging.warning(
                "There is a new version of the bot available. \n"
                "Download the latest release from: \n"
                "https://github.com/stefan2200/TWB"
            )
            time.sleep(5)
        else:
            logging.info("The bot is up-to-date")
