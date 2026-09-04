from __future__ import annotations

import re
import uuid

from streamlink.logger import getLogger
from streamlink.options import Options
from streamlink.plugin import Plugin, PluginError, pluginargument, pluginmatcher
from streamlink.plugin.api import validate

log = getLogger(__name__)


@pluginmatcher(
    name="live",
    pattern=re.compile(
        r"https?://(?:www\.)?m6\.fr/(?P<channel>[^/?#]+)"
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
        "https://layout.6cloud.fr/front/v1/m6web/m6group_web/main/token-web-32/"
        "video/clip_{video_id}/layout?blockPage=1&nbPages=2"
    )
    _LIVE_URL = (
        "https://layout.6cloud.fr/front/v1/m6web/m6group_web/main/token-web-32/"
        "live/{channel}/layout?blockPage=1&nbPages=2"
    )

    _LICENSE_URL = "https://lic.drmtoday.com/license-proxy-widevine/cenc/"

    _LOGIN_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            validate.optional("UID"): str,
            validate.optional("UIDSignature"): str,
            validate.optional("signatureTimestamp"): str,
            validate.optional("errorMessage"): str,
            validate.optional("errorDetails"): str,
        },
        validate.union_get(
            "UID",
            "UIDSignature",
            "signatureTimestamp",
            "errorMessage",
            "errorDetails",
        ),
    )

    _TOKEN_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "token": str,
        },
        validate.get("token"),
    )

    _ASSETS_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "blocks": [dict],
        },
        validate.get("blocks"),
        validate.filter(
            lambda block: block.get("content", {}).get("contentTemplateId") == "Player",
        ),
        validate.get(0),
        validate.get(("content", "items", 0, "itemContent", "video", "assets")),
    )

    _API_KEY = "3_hH5KBv25qZTd_sURpixbQW6a4OsiIzIEF2Ei_2H7TXTGLJb_1Hr4THKZianCQhWK"
    _DEVICE_ID = '_luid_' + str(uuid.UUID(int=uuid.getnode()))
    _PROFILE_ID = "_puid_{account_id}_DEFAULT0"

    _CHANNELS = {
        "m6": "M6",
        "w9": "W9",
        "6ter": "6T",
    }

    @staticmethod
    def _auth_headers(jwt: str) -> dict[str, str]:
        return {
            "X-Customer-Name": "m6web",
            "X-Client-Release": "6.49.0",
            "Authorization": f"Bearer {jwt}",
        }

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

        log.debug("Requesting M6 account authentication")

        account_id, signature, timestamp, error_message, error_details = (
            self.session.http.post(
                self._LOGIN_URL,
                data=payload,
                headers=headers,
                schema=self._LOGIN_SCHEMA,
            )
        )

        if not account_id:
            message = error_message or error_details or "login failed"
            raise PluginError(f"Login failed: {message}")

        log.debug("Authentication successful")

        jwt_headers = {
            "X-Auth-device-id": self._DEVICE_ID,
            "X-Auth-gigya-signature": signature,
            "X-Auth-gigya-signature-timestamp": timestamp,
            "X-Auth-gigya-uid": account_id,
            "X-Auth-profile-id": self._PROFILE_ID.format(account_id=account_id),
            "X-Client-Release": "6.49.0",
            "X-Customer-name": "m6web",
        }

        jwt = self.session.http.get(
            self._JWT_URL,
            headers=jwt_headers,
            schema=self._TOKEN_SCHEMA,
        )

        return account_id, jwt

    def _get_upfront_token(self, url: str, jwt: str) -> str:
        log.debug("Requesting playback token")

        token = self.session.http.get(
            url,
            headers=self._auth_headers(jwt),
            schema=self._TOKEN_SCHEMA,
        )

        log.debug("Resolved playback token")

        return token

    @staticmethod
    def _select_asset(assets, **filters):
        candidates = [
            asset
            for asset in assets
            if all(asset.get(key) == value for key, value in filters.items())
               and asset.get("path")
        ]

        if not candidates:
            return None

        return max(
            candidates,
            key=lambda asset: {"sd": 0, "hd": 1}.get(
                str(asset.get("quality", "")).lower(),
                -1,
            ),
        )["path"]

    def _get_streams(self):
        account_id, jwt = self._get_login_token()

        if self.matches["vod"]:
            self.id = self.match["video_id"]

            if not self.id:
                return

            token_url = self._REPLAY_TOKEN_URL.format(
                account_id=account_id,
                video_id=self.id,
            )
            url = self._VIDEO_URL.format(video_id=self.id)
            provider = "usp"

        elif self.matches["live"]:
            self.id = self.match["channel"]

            if not self.id:
                return

            live_id = self._CHANNELS.get(self.id, self.id)

            token_url = self._LIVE_TOKEN_URL.format(
                account_id=account_id,
                channel=f"dashcenc_{live_id}",
            )
            url = self._LIVE_URL.format(channel=self.id)
            provider = "delta"

        else:
            return

        token = self._get_upfront_token(token_url, jwt)

        assets = self.session.http.get(
            url,
            headers=self._auth_headers(jwt),
            schema=self._ASSETS_SCHEMA,
        )

        manifest = self._select_asset(
            assets,
            provider=provider,
            format="dashcenc",
            container="h264",
        )

        if not manifest:
            raise PluginError("Could not resolve manifest")

        options = Options({
            "license-url": self._LICENSE_URL,
            "license-header": {
                "Host": "lic.drmtoday.com",
                "x-dt-auth-token": token,
            },
            "license-format": "json",
            "license-path": ["license"],
        })

        if device := self.get_option("widevine-device"):
            options.set("device", device)

        yield from self.session.streams(
            f"widevine://{manifest}",
            options=options,
        ).items()


__plugin__ = M6
