from __future__ import annotations

import json
import re
import uuid

from streamlink.logger import getLogger
from streamlink.options import Options
from streamlink.plugin import Plugin, PluginError, pluginargument, pluginmatcher

log = getLogger(__name__)


@pluginmatcher(
    name="live",
    pattern=re.compile(
        r"https?://(?:www\.)?m6\.fr/(?P<channel_name>[^/?#]+)"
    ),
)
@pluginmatcher(
    name="vod",
    pattern=re.compile(
        r"https?://(?:www\.)?m6\.fr/.*?c_(?P<video_id>\d+)"
    ),
)
@pluginargument(
    "username",
    metavar="EMAIL",
    help="M6 account email address.",
    requires=["password"],
)
@pluginargument(
    "password",
    metavar="PASSWORD",
    help="M6 account password.",
    sensitive=True,
)
@pluginargument(
    "widevine-device",
    help="Path to the Widevine device (.wvd) file.",
)
class M6(Plugin):
    _LOGIN_URL = "https://login-gigya.m6.fr/accounts.login"
    _JWT_URL = "https://front-auth.6cloud.fr/v2/platforms/m6group_web/getJwt"

    _REPLAY_TOKEN_URL = (
        "https://drm.6cloud.fr/v1/customers/m6web/platforms/m6group_web/"
        "services/m6replay/users/{account_id}/videos/{video_id}/upfront-token"
    )
    _LIVE_TOKEN_URL = (
        "https://drm.6cloud.fr/v1/customers/m6web/platforms/m6group_web/"
        "services/6play/users/{account_id}/live/{channel}/upfront-token"
    )

    _VIDEO_URL = (
        "https://android.middleware.6play.fr/6play/v2/platforms/"
        "m6group_androidmob/services/6play/videos/{video_id}"
        "?csa=6&with=clips,freemiumpacks"
    )
    _LIVE_URL = (
        "https://android.middleware.6play.fr/6play/v2/platforms/"
        "m6group_androidmob/services/6play/live"
    )

    _LICENSE_URL = (
        "https://lic.drmtoday.com/license-proxy-widevine/cenc/"
        "|User-Agent={user_agent}"
        "&Host=lic.drmtoday.com&x-dt-auth-token={token}"
    )

    _API_KEY = "3_hH5KBv25qZTd_sURpixbQW6a4OsiIzIEF2Ei_2H7TXTGLJb_1Hr4THKZianCQhWK"
    _DEVICE_ID = '_luid_' + str(uuid.UUID(int=uuid.getnode()))

    _CHANNELS = {
        "m6": "M6",
        "w9": "W9",
        "6ter": "6T",
        "rt12": "rtl2",
        "gulli": "gulli",
    }

    @staticmethod
    def _json(resp):
        try:
            return resp.json()
        except (ValueError, json.JSONDecodeError) as err:
            raise PluginError(f"Invalid JSON response: {err}") from err

    def _get_login_token(self) -> tuple[str, str]:
        username = self.get_option("username")
        password = self.get_option("password")

        if not username or not password:
            raise PluginError(
                "M6 requires an account. Set --m6-username and "
                "--m6-password."
            )

        payload = {
            "loginID": username,
            "password": password,
            "APIKey": self._API_KEY,
            "format": "json",
        }
        headers = {
            "Referer": "https://auth.m6.fr/",
        }

        resp = self.session.http.post(self._LOGIN_URL, data=payload, headers=headers)
        data = self._json(resp)

        if "UID" not in data:
            message = data.get("errorMessage") or data.get("errorDetails") or "login failed"
            raise PluginError(f"Login failed: {message}")

        account_id = str(data["UID"])
        signature = data["UIDSignature"]
        timestamp = str(data["signatureTimestamp"])

        jwt_headers = {
            "x-auth-gigya-signature": signature,
            "x-auth-gigya-signature-timestamp": timestamp,
            "x-auth-gigya-uid": account_id,
            "x-auth-device-id": self._DEVICE_ID,
            "x-customer-name": "m6web",
        }

        jwt_resp = self.session.http.get(self._JWT_URL, headers=jwt_headers)
        jwt_data = self._json(jwt_resp)

        try:
            return account_id, jwt_data["token"]
        except KeyError as err:
            raise PluginError("Could not obtain an authentication token") from err

    def _get_upfront_token(self, account_id: str, jwt: str, *, video_id: str | None = None,
                           channel: str | None = None) -> str:
        headers = {
            "X-Customer-Name": "m6web",
            "X-Client-Release": "6.49.0",
            "Authorization": f"Bearer {jwt}",
        }

        if video_id is not None:
            url = self._REPLAY_TOKEN_URL.format(
                account_id=account_id,
                video_id=video_id,
            )
        elif channel is not None:
            url = self._LIVE_TOKEN_URL.format(
                account_id=account_id,
                channel=channel,
            )
        else:
            raise PluginError("Missing token target")

        data = self._json(self.session.http.get(url, headers=headers))

        try:
            return data["token"]
        except KeyError as err:
            raise PluginError("Could not obtain a playback token") from err

    @staticmethod
    def _select_asset(assets, asset_type: str):
        priority = {"sd": 0, "hd": 1}
        manifests = []

        for asset in assets or []:
            if asset.get("type") != asset_type:
                continue

            quality = str(asset.get("video_quality", "")).lower()
            url = asset.get("full_physical_path")
            if not url:
                continue

            if (quality, url) not in manifests:
                manifests.append((quality, url))

        if not manifests:
            return None

        return sorted(
            manifests,
            key=lambda item: priority.get(item[0], -1),
            reverse=True,
        )[0][1]

    def _license_url(self, token: str) -> str:
        return self._LICENSE_URL.format(
            user_agent=self.session.http.headers["User-Agent"],
            token=token,
        )

    def _get_replay_streams(self, video_id: str):
        account_id, jwt = self._get_login_token()
        token = self._get_upfront_token(account_id, jwt, video_id=video_id)

        headers = {
            "x-customer-name": "m6web",
        }
        data = self._json(
            self.session.http.get(
                self._VIDEO_URL.format(video_id=video_id),
                headers=headers,
            )
        )

        try:
            assets = data["clips"][0]["assets"]
        except (KeyError, IndexError, TypeError) as err:
            raise PluginError(f"Unable to resolve video {video_id}") from err

        manifest = self._select_asset(assets, "usp_dashcenc_h264")
        if not manifest:
            raise PluginError("Could not resolve a DASH/CENC replay manifest")

        try:
            response = self.session.http.get(
                manifest,
                allow_redirects=False,
            )
            manifest = response.headers.get("Location", manifest)
        except Exception:
            log.debug("Unable to resolve M6 manifest redirect", exc_info=True)

        options = Options({
            "license-server": self._license_url(token),
        })

        if device := self.get_option("widevine-device"):
            options.set("device", device)

        return self.session.streams(
            f"widevine://{manifest}",
            options=options,
        ).items()

    def _get_live_streams(self, channel: str):
        account_id, jwt = self._get_login_token()

        live_id = self._CHANNELS[channel]
        token = self._get_upfront_token(account_id, jwt, channel=f"dashcenc_{live_id}")

        params = {
            "channel": live_id,
            "with": "service_display_images,nextdiffusion,extra_data",
        }
        data = self._json(
            self.session.http.get(
                self._LIVE_URL,
                params=params,
            )
        )

        try:
            assets = data[live_id][0]["live"]["assets"]
        except (KeyError, IndexError, TypeError) as err:
            raise PluginError(f"Unable to resolve live channel {channel}") from err

        manifest = self._select_asset(assets, "delta_dashcenc_h264")
        if not manifest:
            raise PluginError("Could not resolve a DASH/CENC live manifest")

        options = Options({
            "license-server": self._license_url(token),
        })

        if device := self.get_option("widevine-device"):
            options.set("device", device)

        return self.session.streams(
            f"widevine://{manifest}",
            options=options,
        ).items()

    def _get_streams(self):
        if self.matches["vod"]:
            self.id = self.match["video_id"]
            if self.id:
                yield from self._get_replay_streams(self.id)
        elif self.matches["live"]:
            self.id = self.match["channel_name"]
            if self.id:
                yield from self._get_live_streams(self.id)


__plugin__ = M6
