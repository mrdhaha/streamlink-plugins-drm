from __future__ import annotations

import json
import re
import uuid
from urllib.parse import urlparse

from streamlink.logger import getLogger
from streamlink.options import Options
from streamlink.plugin import Plugin, PluginError, pluginargument, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.stream.dash import DASHStream
from streamlink.stream.hls import HLSStream
from streamlink.stream.http import HTTPStream

log = getLogger(__name__)


@pluginmatcher(
    name="live",
    pattern=re.compile(
        r"https?://(?:www\.)?auvio\.rtbf\.be/live/"
        r"(?P<slug>[^/?#]+)-(?P<video_id>\d+)"
        r"(?:[/?#].*)?$",
        re.IGNORECASE,
    ),
)
@pluginmatcher(
    name="media",
    pattern=re.compile(
        r"https?://(?:www\.)?auvio\.rtbf\.be/media/"
        r"(?P<slug>[^/?#]+)-(?P<video_id>\d+)"
        r"(?:[/?#].*)?$",
        re.IGNORECASE,
    ),
)
@pluginargument(
    "username",
    metavar="EMAIL",
    help="RTBF/Auvio account email address.",
    requires=["password"],
)
@pluginargument(
    "password",
    metavar="PASSWORD",
    help="RTBF/Auvio account password.",
    sensitive=True,
)
@pluginargument(
    "widevine-device",
    help="Path to the Widevine device (.wvd) file.",
)
class RTBF(Plugin):
    _REDBEE_BASE_URL = "https://exposure.api.redbee.live/v2/customer/RTBF/businessunit/Auvio"
    _REDBEE_SESSION_URL = f"{_REDBEE_BASE_URL}/auth/gigyaLogin"
    _REDBEE_ENTITLEMENT_URL = (
        f"{_REDBEE_BASE_URL}/entitlement/{{stream_id}}/play"
    )

    _BFF_URL = "https://bff-service.rtbf.be/auvio/v1.23/pages/live/{slug}-{video_id}"

    _LOGIN_URL = "https://login.auvio.rtbf.be/accounts.login"
    _JWT_URL = "https://login.auvio.rtbf.be/accounts.getJWT"

    _API_KEY = "4_Ml_fJ47GnBAW6FrPzMxh0w"

    _LOGIN_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "errorCode": int,
            "statusCode": int,
            validate.optional("errorMessage"): str,
            validate.optional("sessionInfo"): {
                "cookieValue": str,
            },
        },
    )

    _JWT_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "errorCode": int,
            "statusCode": int,
            validate.optional("errorMessage"): str,
            "id_token": str,
        },
        validate.get("id_token"),
    )

    _LIVE_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "data": {
                "content": {
                    "streamId": str,
                },
            },
        },
        validate.get(("data", "content", "streamId")),
    )

    _REDBEE_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "sessionToken": str,
        },
        validate.get("sessionToken"),
    )

    _ENTITLEMENT_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "formats": [dict],
        },
        validate.get("formats"),
    )

    def _get_rtbf_login_token(self) -> str:
        username = self.get_option("username")
        password = self.get_option("password")

        if not username or not password:
            raise PluginError(
                "RTBF/Auvio requires an account. "
                "Set --rtbf-username and --rtbf-password."
            )

        params = {
            "loginID": username,
            "password": password,
            "lang": "fr",
            "APIKey": self._API_KEY,
            "format": "json",
        }

        response = self.session.http.get(
            self._LOGIN_URL,
            params=params,
        )

        data = self._LOGIN_SCHEMA.validate(response.text)

        if data["errorCode"] != 0:
            raise PluginError(
                f"RTBF login failed: {data.get('errorMessage', 'unknown error')}"
            )

        if data["statusCode"] != 200:
            raise PluginError(
                f"RTBF login failed with status code {data['statusCode']}"
            )

        return data["sessionInfo"]["cookieValue"]

    def _get_rtbf_id_token(self, login_token: str) -> str:
        params = {
            "APIKey": self._API_KEY,
            "login_token": login_token,
            "format": "json",
        }

        response = self.session.http.get(
            self._JWT_URL,
            params=params,
        )

        data = self._JWT_SCHEMA.validate(response.text)

        return data

    def _get_live_stream_id(self, slug, video_id):
        response = self.session.http.get(
            self._BFF_URL.format(slug=slug, video_id=video_id),
            params={"userAgent": "Chrome-web-3.0"},
        )

        return self._LIVE_SCHEMA.validate(response.text)

    def _get_redbee_session_token(self) -> str:
        login_token = self._get_rtbf_login_token()
        id_token = self._get_rtbf_id_token(login_token)

        payload = {
            "device": {
                "deviceId": str(uuid.uuid4()),
                "name": "Browser",
                "type": "WEB",
            },
            "jwt": id_token,
        }

        response = self.session.http.post(
            self._REDBEE_SESSION_URL,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload),
        )

        return self._REDBEE_SCHEMA.validate(response.text)

    def _get_entitlement(self, stream_id: str, session_token: str):
        params = {
            "supportedFormats": "hls,dash,mss,mp3",
            "supportedDrms": "widevine",
        }

        response = self.session.http.get(
            self._REDBEE_ENTITLEMENT_URL.format(stream_id=stream_id),
            params=params,
            headers={"Authorization": f"Bearer {session_token}"},
        )

        return self._ENTITLEMENT_SCHEMA.validate(response.text)

    @staticmethod
    def _get_format(formats):
        priority = {
            "": 0,
            "mss": 1,
            "aac": 2,
            "mp3": 3,
            "dash": 4,
            "hls": 5,
        }

        formats = sorted(
            formats,
            key=lambda fmt: priority.get(
                fmt.get("format", "").lower(),
                0,
            ),
            reverse=True,
        )

        for fmt in formats:
            if not fmt.get("format"):
                continue

            drm = fmt.get("drm") or {}

            if not drm:
                return fmt, None

            for key, value in drm.items():
                if "widevine" not in key.lower():
                    continue

                license_url = value.get("licenseServerUrl")
                if license_url:
                    return fmt, license_url

        return None, None

    def _get_streams(self):
        video_id = self.match["video_id"]

        if self.matches["live"]:
            stream_id = self._get_live_stream_id(
                self.match["slug"],
                video_id,
            )
        else:
            stream_id = video_id

        session_token = self._get_redbee_session_token()
        formats = self._get_entitlement(stream_id, session_token)

        if not formats:
            raise PluginError("No playable formats returned by RedBee")

        video_format, license_url = self._get_format(formats)

        if not video_format:
            raise PluginError("Could not find a supported RTBF stream")

        url = video_format.get("mediaLocator")

        if not url:
            raise PluginError("RTBF returned a format without a media locator")

        format_name = video_format.get("format", "").lower()

        log.debug(
            "Selected RTBF format: %s (%s)",
            format_name,
            url,
        )

        self.id = video_id

        if license_url:
            options = Options({
                "license-url": license_url,
            })

            if device := self.get_option("widevine-device"):
                options.set("device", device)

            yield from self.session.streams(
                f"widevine://{url}",
                options=options,
            ).items()
        else:
            path = urlparse(url).path

            if path.endswith(".m3u8"):
                log.debug("Resolved HLS stream: %s", url)
                streams = HLSStream.parse_variant_playlist(self.session, url).items()
            elif path.endswith(".mpd"):
                log.debug("Resolved DASH stream: %s", url)
                streams = DASHStream.parse_manifest(self.session, url).items()
            elif path.endswith(".mp3"):
                log.debug("Resolved MP3 stream: %s", url)
                streams = [("audio", HTTPStream(self.session, url))]
            else:
                raise PluginError(f"Unsupported stream URL format: {url}")

            yield from streams


__plugin__ = RTBF