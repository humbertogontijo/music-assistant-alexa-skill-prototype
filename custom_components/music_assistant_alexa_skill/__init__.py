"""Bridge Alexa skill requests through a Nabu Casa cloud webhook."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from homeassistant.components import webhook
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ADDON_PORT, DOMAIN, WEBHOOK_ID

_LOGGER = logging.getLogger(__name__)

_HOP_BY_HOP = {"host", "content-length", "transfer-encoding", "connection"}

_DATA_ADDON_URL = "addon_base_url"
_DATA_CLOUDHOOK_URL = "cloudhook_url"
_DATA_VIEW = "view_registered"


def _valid_addon_url(url: str) -> bool:
    """Accept only the add-on's internal HTTP address."""
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        return False
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return False
    if parsed.port != ADDON_PORT:
        return False
    host = parsed.hostname or ""
    return bool(host) and host not in {"localhost", "127.0.0.1", "::1"}


async def _cloudhook_url(hass: HomeAssistant) -> str | None:
    """Return the Nabu Casa URL for this skill, creating it when needed."""
    try:
        from homeassistant.components import cloud
    except ImportError:
        return None
    if not hasattr(cloud, "async_get_or_create_cloudhook"):
        return None
    try:
        return await cloud.async_get_or_create_cloudhook(hass, WEBHOOK_ID)
    except Exception:
        _LOGGER.exception("Could not create a Nabu Casa cloud webhook")
        return None


async def _handle_webhook(hass: HomeAssistant, webhook_id: str, request: web.Request) -> web.Response:
    """Forward an Alexa request to the add-on and return its response."""
    base = hass.data.get(DOMAIN, {}).get(_DATA_ADDON_URL)
    if not base:
        return web.Response(status=503, text="Music Assistant Alexa add-on is not registered")

    body = await request.read()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in _HOP_BY_HOP
    }
    session = async_get_clientsession(hass)
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with session.post(f"{base}/", data=body, headers=headers, timeout=timeout) as resp:
            payload = await resp.read()
            return web.Response(
                status=resp.status,
                body=payload,
                content_type=resp.content_type or "application/json",
            )
    except aiohttp.ClientError:
        _LOGGER.exception("Music Assistant Alexa add-on did not respond")
        return web.Response(status=502, text="Music Assistant Alexa add-on did not respond")


class CloudhookView(HomeAssistantView):
    """Let the add-on publish its address and read the Nabu Casa URL."""

    url = "/api/music_assistant_alexa_skill/cloudhook"
    name = "api:music_assistant_alexa_skill:cloudhook"

    async def get(self, request: web.Request) -> web.Response:
        return await self._response(request, None)

    async def post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return await self._response(request, data.get("addon_base_url"))

    async def _response(self, request: web.Request, addon_base_url: str | None) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        user = request["hass_user"]
        if user is None or not user.is_admin:
            return self.json({"error": "admin required"}, status_code=403)

        store = hass.data.setdefault(DOMAIN, {})
        if addon_base_url:
            if not _valid_addon_url(addon_base_url):
                return self.json({"error": "invalid addon_base_url"}, status_code=400)
            store[_DATA_ADDON_URL] = addon_base_url.rstrip("/")
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                hass.config_entries.async_update_entry(
                    entries[0],
                    options={**entries[0].options, _DATA_ADDON_URL: store[_DATA_ADDON_URL]},
                )

        url = await _cloudhook_url(hass)
        if not url:
            return self.json(
                {"error": "Home Assistant Cloud is not connected"},
                status_code=503,
            )
        store[_DATA_CLOUDHOOK_URL] = url
        return self.json(
            {
                "cloudhook_url": url,
                "addon_base_url": store.get(_DATA_ADDON_URL, ""),
            }
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Register the webhook and the add-on API."""
    store = hass.data.setdefault(DOMAIN, {})
    saved = entry.options.get(_DATA_ADDON_URL)
    if isinstance(saved, str) and _valid_addon_url(saved):
        store[_DATA_ADDON_URL] = saved.rstrip("/")

    try:
        webhook.async_unregister(hass, WEBHOOK_ID)
    except KeyError:
        pass
    webhook.async_register(
        hass,
        DOMAIN,
        "Music Assistant Alexa Skill",
        WEBHOOK_ID,
        _handle_webhook,
        local_only=False,
        allowed_methods=["POST"],
    )
    if not store.get(_DATA_VIEW):
        hass.http.register_view(CloudhookView())
        store[_DATA_VIEW] = True

    url = await _cloudhook_url(hass)
    if url:
        store[_DATA_CLOUDHOOK_URL] = url
        _LOGGER.info("Alexa skill Nabu Casa URL is %s", url)
    else:
        _LOGGER.warning(
            "Home Assistant Cloud is not connected, so no public skill URL is available"
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Drop the webhook registration."""
    webhook.async_unregister(hass, WEBHOOK_ID)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the cloud webhook when the integration is deleted."""
    try:
        from homeassistant.components import cloud
    except ImportError:
        return
    delete = getattr(cloud, "async_delete_cloudhook", None)
    if delete is None:
        return
    try:
        await delete(hass, WEBHOOK_ID)
    except Exception:
        _LOGGER.debug("Cloud webhook %s was already gone", WEBHOOK_ID)
