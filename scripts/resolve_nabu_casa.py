#!/usr/bin/env python3
"""Resolve Nabu Casa URLs for the Alexa skill endpoint and the audio stream.

Prints shell exports on success. Home Assistant Cloud must be connected,
and this add-on must be allowed to call the Home Assistant API. When
remote access is on and MA_HOSTNAME is empty, the stream export is the
Nabu Casa remote address plus the local stream proxy path.
"""
import json
import os
import shlex
import socket
import sys
import time
import urllib.error
import urllib.request

# /core/api is Supervisor's proxy for Home Assistant's /api.
# /core/api/states is /api/states, so these paths must not repeat /api.
API = "http://supervisor/core/api"
DOMAIN = "music_assistant_alexa_skill"
PORT = 5000
CLOUDHOOK_PATH = f"/{DOMAIN}/cloudhook"
FLOW_PATH = "/config/config_entries/flow"


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
        CLOUDHOOK_PATH,
        {"addon_base_url": _addon_base_url()},
    )
    return status, body


def _ensure_config_entry():
    """Create the integration entry when Home Assistant has loaded it."""
    try:
        status, body = _request(
            "POST",
            FLOW_PATH,
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
                f"{FLOW_PATH}/{flow_id}",
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


def exports_for(body, existing_ma_hostname=""):
    """Shell lines to eval, plus warnings that should not abort startup."""
    lines = []
    warnings = []
    url = (body.get("cloudhook_url") or "").strip()
    if url.startswith("https://"):
        lines.append(f"export SKILL_HOSTNAME={shlex.quote(url)}")
    stream = (body.get("remote_stream_url") or "").strip()
    if stream.startswith("https://") and not (existing_ma_hostname or "").strip():
        lines.append(f"export MA_HOSTNAME={shlex.quote(stream)}")
    elif not (existing_ma_hostname or "").strip():
        if body.get("remote_enabled") and not body.get("remote_connected"):
            warnings.append(
                "Home Assistant Cloud remote access is enabled but not connected yet, "
                "so MA_HOSTNAME was left empty."
            )
        elif not body.get("remote_enabled"):
            warnings.append(
                "Turn on Home Assistant Cloud remote access so an Echo can download audio, "
                "or set MA_HOSTNAME to your own public stream host."
            )
    if stream.startswith("https://") and not (body.get("ma_stream_base") or "").strip():
        warnings.append(
            "Music Assistant add-on was not found or is not started. "
            "Audio forwarding starts once that add-on is running."
        )
    return lines, warnings


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
                if _ensure_config_entry():
                    continue
                last_error = (
                    "The Music Assistant Alexa Skill integration is not loaded yet. "
                    "Restart Home Assistant once, then start this add-on again. "
                    "Home Assistant Cloud must be signed in."
                )
            else:
                last_error = f"HTTP {err.code}: {detail}"
                if err.code in (401, 403, 503):
                    print(last_error, file=sys.stderr)
                    return 1
        except urllib.error.URLError as err:
            last_error = str(err.reason)
        except Exception as err:
            last_error = str(err)
        else:
            lines, warnings = exports_for(body, os.environ.get("MA_HOSTNAME", ""))
            if lines:
                waiting_for_remote = (
                    body.get("remote_enabled")
                    and not body.get("remote_connected")
                    and not (os.environ.get("MA_HOSTNAME") or "").strip()
                    and attempt < 5
                )
                if waiting_for_remote:
                    time.sleep(3)
                    continue
                print("\n".join(lines))
                url = (body.get("cloudhook_url") or "").strip()
                stream = (body.get("remote_stream_url") or "").strip()
                if url:
                    print(f"Nabu Casa skill URL: {url}", file=sys.stderr)
                if stream and not (os.environ.get("MA_HOSTNAME") or "").strip():
                    print(f"Nabu Casa stream URL: {stream}", file=sys.stderr)
                for warning in warnings:
                    print(warning, file=sys.stderr)
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
