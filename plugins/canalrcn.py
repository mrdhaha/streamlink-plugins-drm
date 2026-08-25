import re

from streamlink.logger import getLogger
from streamlink.options import Options
from streamlink.plugin import Plugin, pluginargument, pluginmatcher
from streamlink.plugin.api import validate
from streamlink.utils.url import url_concat


log = getLogger(__name__)


BASE_URL = "https://www.canalrcn.com"
HEADERS = {
    "Referer": f"{BASE_URL}/",
    "Origin": BASE_URL,
}


@pluginmatcher(
    re.compile(
        r"https?://(?:www\.)?canalrcn\.com/[^/]+/player/(?P<id>[^/?#]+)"
    )
)
@pluginargument(
    "widevine-device",
    help="Path to the Widevine device (.wvd) file.",
)
class CanalRCN(Plugin):
    _CONFIG_SCHEMA = validate.Schema(
        re.compile(
            r"""
            TBX_CLIENT_KEY:\s*'(?P<client_key>[^']+)'.*?
            TBX_UNITY_API:\s*'(?P<unity_api>[^']+)'
            """,
            re.DOTALL | re.VERBOSE,
        ),
        validate.transform(
            lambda match: {
                "client_key": match.group("client_key"),
                "unity_api": match.group("unity_api"),
            },
        ),
        {
            "client_key": str,
            "unity_api": validate.url(
                hostname="unity.tbxapis.com",
            ),
        },
        validate.union_get(
            "client_key",
            "unity_api",
        ),
    )
    
    _TOKEN_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "token": {
                "access_token": str,
            },
        },
        validate.get("token"),
        validate.get("access_token"),
    )
    
    _ENTITLEMENT_SCHEMA = validate.Schema(
        validate.filter(
            lambda e: e.get("contentType") == "application/dash+xml",
        ),
        validate.get(0),
        {
            "url": validate.url(
                hostname=validate.endswith(".cdn.broadpeak.io"),
                path=validate.endswith(".mpd"),
            ),
            "drm": {
                "widevine": {
                    "licenseAcquisitionUrl": validate.url(
                        hostname="cpix.tbxdrm.com",
                    ),
                },
            },
        },
        validate.union_get(
            "url",
            ("drm", "widevine", "licenseAcquisitionUrl"),
        ),
    )
    
    _METADATA_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "entitlements": [dict],
        },
        validate.get("entitlements"),
        _ENTITLEMENT_SCHEMA,
    )
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.session.http.headers.update(HEADERS)

    def _get_streams(self):
        log.debug("Loading site configuration")
        client_key, unity_api = self.session.http.get(
            url_concat(BASE_URL, "env-config.js"),
            schema=self._CONFIG_SCHEMA,
        )
        
        log.debug("Requesting public API token")
        token = self.session.http.post(
            url_concat(unity_api, "auth", "public"),
            json={
                "auth": {
                    "sub": client_key,
                    "country": "CO",
                    "language": "es",
                },
            },
            schema=self._TOKEN_SCHEMA,
        )
        
        log.debug("Requesting metadata for content ID: %s", self.match["id"])
        mpd_url, license_url = self.session.http.get(
            url_concat(unity_api, "contents", self.match["id"], "url"),
            headers={
                "Authorization": f"JWT {token}",
            },
            schema=self._METADATA_SCHEMA,
        )
        log.debug("Resolved DASH manifest: %s", mpd_url)
        log.debug("Resolved Widevine license server: %s", license_url)
        log.debug("Delegating to Widevine plugin")
        options = {
            "license-server": license_url,
        }
        if device := self.get_option("widevine-device"):
            options["device"] = device
        return self.session.streams(
            f"widevine://{mpd_url}",
            options=Options(options),
        )


__plugin__ = CanalRCN
