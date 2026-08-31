"""HTTP-клиент коллекторов: вежливый, кеширующий, с повторами.

Правило вежливости не только этика: заблокированный по IP коллектор — это
отказ источника. Поэтому осмысленный User-Agent с контактом, уважение
Retry-After и кеш по ETag.
"""
import time
from dataclasses import dataclass

import httpx

# Контакт для владельцев сайтов — ссылка, а не личная почта: репозиторий
# публичный, и адрес из User-Agent осел бы во всех логах и у сборщиков спама.
USER_AGENT = ("MonitoringMap/1.0 "
              "(+https://github.com/konkindavid-glitch/wb-monitoring) "
              "seller-news monitoring")
TIMEOUT = 20.0
RETRIES = 3


@dataclass(frozen=True)
class FetchResult:
    status: int
    text: str = ""
    etag: str | None = None
    last_modified: str | None = None
    from_cache: bool = False


class Fetcher:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
            follow_redirects=True)

    def get(self, url: str, etag=None, last_modified=None) -> FetchResult:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        delay = 2.0
        for attempt in range(RETRIES):
            try:
                response = self._client.get(url, headers=headers)
            except httpx.HTTPError:
                if attempt == RETRIES - 1:
                    return FetchResult(0)
                time.sleep(delay)
                delay *= 2
                continue

            if response.status_code == 304:
                return FetchResult(304, from_cache=True)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == RETRIES - 1:
                    return FetchResult(response.status_code)
                wait = float(response.headers.get("Retry-After", delay))
                time.sleep(wait)
                delay *= 2
                continue

            return FetchResult(response.status_code, response.text,
                               response.headers.get("ETag"),
                               response.headers.get("Last-Modified"))
        return FetchResult(0)

    def close(self) -> None:
        self._client.close()
