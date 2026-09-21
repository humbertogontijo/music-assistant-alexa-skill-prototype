#!/usr/bin/env python3
"""Resolve a Nabu Casa cloud webhook URL for the Alexa skill endpoint.

Prints a shell export for SKILL_HOSTNAME on success. Home Assistant Cloud
must be connected, and this add-on must be allowed to call the Home
Assistant API.
"""
import json
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request

API = "http://supervisor/core/api"
DOMAIN = "music_assistant_alexa_skill"
PORT = 5000


def _token():
    import os
    return (os.environ.get("SUPERVISOR_TOKEN") or "").strip()


def _request(method, path, payload=None):
    token = _token()
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        API + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read().decode("utf-8")
        if not raw:
            return resp.status, {}
        return resp.status, json.loads(raw)


def _addon_base_url():
    host = socket.gethostname().strip()
    return f"http://{host}:{PORT}"


def _register():
    status, body = _request(
        "POST",
        f"/api/{DOMAIN}/cloudhook",
        {"addon_base_url": _addon_base_url()},
    )
    return status, body


def _ensure_config_entry():
    """Create the integration entry when Home Assistant has loaded it."""
    try:
        status, body = _request(
            "POST",
            "/api/config/config_entries/flow",
            {"handler": DOMAIN},
        )
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        detail = err.read().decode("utf-8", errors="replace")
        print(f"Config flow failed: {err.code} {detail}", file=sys.stderr)
        return False

    if body.get("type") == "create_entry":
        return True
    if body.get("type") == "abort" and body.get("reason") == "single_instance_allowed":
        return True
    flow_id = body.get("flow_id")
    if body.get("type") == "form" and flow_id:
        try:
            _, stepped = _request(
                "POST",
                f"/api/config/config_entries/flow/{flow_id}",
                {},
            )
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            print(f"Config flow step failed: {err.code} {detail}", file=sys.stderr)
            return False
        if stepped.get("type") in ("create_entry", "abort"):
            return stepped.get("type") == "create_entry" or stepped.get("reason") == "single_instance_allowed"
    print(f"Unexpected config flow result: {body}", file=sys.stderr)
    return status == 200 and body.get("type") == "create_entry"


def main():
    if not _token():
        print(
            "SUPERVISOR_TOKEN is missing, so this add-on cannot ask Home Assistant for a Nabu Casa URL.",
            file=sys.stderr,
        )
        return 1

    last_error = "Home Assistant API did not respond"
    for attempt in range(1, 6):
        try:
            _, body = _register()
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            if err.code == 404:
                if not _ensure_config_entry():
                    print(
                        "The Music Assistant Alexa Skill integration is not loaded yet. "
                        "Restart Home Assistant once, then start this add-on again. "
                        "Home Assistant Cloud must be signed in.",
                        file=sys.stderr,
                    )
                    return 1
                continue
            last_error = f"HTTP {err.code}: {detail}"
            if err.code in (401, 403, 503):
                print(last_error, file=sys.stderr)
                return 1
        except urllib.error.URLError as err:
            last_error = str(err.reason)
        except Exception as err:
            last_error = str(err)
        else:
            url = (body.get("cloudhook_url") or "").strip()
            if url.startswith("https://"):
                print(f"export SKILL_HOSTNAME={shlex.quote(url)}")
                print(f"Nabu Casa skill URL: {url}", file=sys.stderr)
                return 0
            last_error = body.get("error") or "cloud webhook URL missing"
            if "not connected" in last_error.lower():
                print(last_error, file=sys.stderr)
                return 1
        if attempt < 5:
            time.sleep(3)

    print(f"Could not get a Nabu Casa skill URL: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
