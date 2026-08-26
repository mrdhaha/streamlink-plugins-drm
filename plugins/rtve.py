"""
Copyright (c) 2011-2016, Christopher Rosell
Copyright (c) 2016-2026, Streamlink Team
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
POSSIBILITY OF SUCH DAMAGE.
"""

from __future__ import annotations

import re
from base64 import b64decode
from io import BytesIO
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from streamlink.logger import getLogger
from streamlink.options import Options
from streamlink.plugin import Plugin, PluginError, pluginmatcher, pluginargument
from streamlink.plugin.api import validate
from streamlink.stream.dash import DASHStream
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.stream.hls import HLSStream
from streamlink.stream.http import HTTPStream
from streamlink.utils.url import update_scheme, url_concat

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


log = getLogger(__name__)


class Base64Reader:
    def __init__(self, data: str):
        stream = BytesIO(b64decode(data))

        def _iterate():
            while True:
                chunk = stream.read(1)
                if len(chunk) == 0:
                    return
                yield ord(chunk)

        self._iterator: Iterator[int] = _iterate()

    def read(self, num: int) -> Sequence[int]:
        res = []
        for _ in range(num):
            item = next(self._iterator, None)
            if item is None:
                break
            res.append(item)
        return res

    def skip(self, num: int) -> None:
        self.read(num)

    def read_chars(self, num: int) -> str:
        return "".join(chr(item) for item in self.read(num))

    def read_int(self) -> int:
        a, b, c, d = self.read(4)
        return a << 24 | b << 16 | c << 8 | d

    def read_chunk(self) -> tuple[str, Sequence[int]]:
        size = self.read_int()
        chunktype = self.read_chars(4)
        chunkdata = self.read(size)
        if len(chunkdata) != size:  # pragma: no cover
            raise ValueError("Invalid chunk length")
        self.skip(4)
        return chunktype, chunkdata

    def __iter__(self):
        self.skip(8)
        while True:
            try:
                yield self.read_chunk()
            except ValueError:
                return


class ZTNR:
    @staticmethod
    def _get_alphabet(text: str) -> str:
        res = []
        j = 0
        k = 0
        for char in text:
            if k > 0:
                k -= 1
            else:
                res.append(char)
                j = (j + 1) % 4
                k = j
        return "".join(res)

    @staticmethod
    def _get_url(text: str, alphabet: str) -> str:
        res = []
        j = 0
        n = 0
        k = 3
        cont = 0
        for char in text:
            if j == 0:
                n = int(char) * 10
                j = 1
            elif k > 0:
                k -= 1
            else:
                res.append(alphabet[n + int(char)])
                j = 0
                k = cont % 4
                cont += 1
        return "".join(res)

    @classmethod
    def _get_source(cls, alphabet: str, data: str) -> str:
        return cls._get_url(data, cls._get_alphabet(alphabet))

    @classmethod
    def translate(cls, data: str) -> Iterator[tuple[str, str]]:
        reader = Base64Reader(data.replace("\n", ""))
        for chunk_type, chunk_data in reader:
            if chunk_type == "IEND":
                break
            if chunk_type == "tEXt":
                content = "".join(chr(item) for item in chunk_data if item > 0)
                if "#" not in content:
                    continue
                alphabet, content = content.split("#", 1)
                if "%%" in content:
                    quality, content = content.split("%%", 1)
                else:
                    quality = ""
                yield quality, cls._get_source(alphabet, content)


@pluginmatcher(
    re.compile(r"https?://(?:www\.)?rtve\.es/play/(?:videos|clan)/.+"),
)
@pluginargument(
    "widevine-device",
    help="Path to the Widevine device (.wvd) file.",
)
class Rtve(Plugin):
    _ZTNR_DOMAIN = "https://ztnr.rtve.es"
    _API_DOMAIN = "https://api.rtve.es"

    _M3U8_URL = url_concat(_ZTNR_DOMAIN, "/ztnr/{id}.m3u8")
    _MPD_URL = url_concat(_ZTNR_DOMAIN, "/ztnr/{id}.mpd")
    _THUMBNAIL_URL = url_concat(_ZTNR_DOMAIN, "/ztnr/movil/thumbnail/rtveplayw/videos/{id}.png?q=v2")
    _API_VIDEOS_URL = url_concat(_API_DOMAIN, "/api/videos/{id}.json")
    _API_TOKEN_URL = url_concat(_API_DOMAIN, "/api/token/{id}")

    _DATA_SETUP_SCHEMA = validate.Schema(
        validate.xml_xpath_string(
            ".//*[contains(@class,'videoPlayer')][@data-setup][1]/@data-setup"
        ),
        validate.parse_json(),
        {
            "idAsset": validate.any(
                int,
                validate.all(str, validate.transform(int)),
            ),
            "hasDRM": bool,
        },
        validate.union_get("idAsset", "hasDRM"),
    )

    _IS_VOD_SCHEMA = validate.Schema(
        validate.xml_xpath_string(
            ".//link[@rel='stylesheet']["
            "contains(@href, 'rtve.play.pf_video.') or "
            "contains(@href, 'rtve.play.pf_directo.')"
            "][1]/@href"
        ),
        validate.any(
            validate.all(
                validate.contains("rtve.play.pf_video."),
                validate.transform(lambda _: True),
            ),
            validate.all(
                validate.contains("rtve.play.pf_directo."),
                validate.transform(lambda _: False),
            ),
        ),
    )

    _TOKEN_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "widevineURL": validate.url(),
        },
        validate.get("widevineURL"),
    )

    _SUBTITLE_REF_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "page": {
                "items": [
                    {
                        validate.optional("subtitleRef"): validate.url(),
                    },
                ],
            },
        },
        validate.get(("page", "items", 0, "subtitleRef"), None),
    )

    _SUBTITLE_SCHEMA = validate.Schema(
        validate.parse_json(),
        {
            "page": {
                "items": [
                    {
                        "lang": str,
                        "src": validate.url(),
                    },
                ],
            },
        },
        validate.get(("page", "items")),
    )

    def _mux_subtitles(self, streams):
        log.debug("Subtitle muxing enabled")
        try:
            subtitle_ref = self.session.http.get(
                self._API_VIDEOS_URL.format(id=self.id),
                schema=self._SUBTITLE_REF_SCHEMA,
            )
        except PluginError:
            log.warning("Unable to retrieve video metadata for subtitles")
            return streams

        if not subtitle_ref:
            log.debug("No subtitle reference found")
            return streams

        log.debug("Resolved subtitle reference: %s", subtitle_ref)
        try:
            subs = self.session.http.get(
                f"{subtitle_ref}.json",
                schema=self._SUBTITLE_SCHEMA,
            )
        except PluginError:
            log.warning("Unable to retrieve subtitles")
            return streams

        if not subs:
            log.debug("No subtitles available")
            return streams

        subtitles = {
            s["lang"]: HTTPStream(self.session, update_scheme("https://", s["src"], force=True))
            for s in subs
        }  # fmt: skip

        log.debug(
            "Resolved %d subtitle track(s): %s",
            len(subs),
            ", ".join(subtitles),
        )

        log.debug("Muxing %d subtitle track(s) into streams", len(subtitles))
        return [
            (
                quality,
                MuxedStream(
                    self.session,
                    stream,
                    subtitles=subtitles,
                ),
            )
            for quality, stream in streams
        ]

    def _get_streams(self):
        (self.id, has_drm), is_vod = self.session.http.get(
            self.url,
            schema=validate.Schema(
                validate.parse_html(),
                validate.union((
                    self._DATA_SETUP_SCHEMA,
                    self._IS_VOD_SCHEMA,
                )),
            ),
        )
        log.debug(
            "Resolved asset: id=%s, DRM=%s, VOD=%s",
            self.id,
            has_drm,
            is_vod,
        )

        if has_drm:
            log.debug("DRM content, requesting Widevine license URL")
            license_url = self.session.http.get(
                self._API_TOKEN_URL.format(id=self.id),
                schema=self._TOKEN_SCHEMA,
            )
            log.debug("Resolved Widevine license server: %s", license_url)

            url = self._MPD_URL.format(id=self.id)
            log.debug("Using DASH manifest: %s", url)

            options = {
                "license-server": license_url,
            }
            if device := self.get_option("widevine-device"):
                options["device"] = device
            streams = self.session.streams(
                f"widevine://{url}",
                options=Options(options),
            ).items()

        elif not is_vod:
            url = self._M3U8_URL.format(id=self.id)
            log.debug("Live content, using HLS manifest: %s", url)
            streams = HLSStream.parse_variant_playlist(self.session, url).items()

        else:
            log.debug("VOD content, resolving ZTNR thumbnail")
            try:
                urls = self.session.http.get(
                    self._THUMBNAIL_URL.format(id=self.id),
                    schema=validate.Schema(
                        validate.transform(ZTNR.translate),
                        validate.transform(list),
                        [(str, validate.url())],
                        validate.length(1),
                    ),
                )
            except PluginError:
                # catch HTTP errors and validation errors, and fall back to generic HLS URL template
                url = self._M3U8_URL.format(id=self.id)
                log.debug("ZTNR media metadata unavailable, falling back to HLS manifest: %s", url)
                streams = HLSStream.parse_variant_playlist(self.session, url).items()
            else:
                log.debug("Requesting ZTNR media metadata: %s", self._THUMBNAIL_URL.format(id=self.id))
                quality, url = urls[0]
                log.debug("Resolved ZTNR media URL (%s): %s", quality or "unknown", url)

                path = urlparse(url).path

                if path.endswith(".m3u8"):
                    log.debug("Resolved HLS stream: %s", url)
                    streams = HLSStream.parse_variant_playlist(self.session, url).items()
                elif path.endswith(".mpd"):
                    log.debug("Resolved DASH stream: %s", url)
                    streams = DASHStream.parse_manifest(self.session, url).items()
                elif path.endswith(".mp4"):
                    log.debug("Resolved MP4 stream: %s", url)
                    streams = [(quality or "vod", HTTPStream(self.session, url))]
                else:
                    raise PluginError(f"Unsupported stream URL format: {url}")

        if self.session.get_option("mux-subtitles"):
            streams = self._mux_subtitles(streams)

        yield from streams


__plugin__ = Rtve
