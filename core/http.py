"""Client HTTP robuste : rate limiting (token bucket), retries exponentiels avec jitter,
respect de Retry-After, pagination générique et cache disque conditionnel (ETag)."""
from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import requests

from config.settings import get_settings

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 522, 524}


class HttpError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, url: str | None = None):
        super().__init__(message)
        self.status = status
        self.url = url


class NotFound(HttpError):
    """404 : ressource absente (ex. fichier inexistant pour une saison ancienne)."""


class TokenBucket:
    """Limiteur de débit thread-safe : `rate` jetons/s, capacité `burst`."""

    def __init__(self, rate: float, burst: int = 1):
        if rate <= 0:
            raise ValueError("rate doit être > 0")
        self.rate = rate
        self.capacity = max(1, burst)
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Vide le seau après un 429 pour ralentir tous les threads."""
        with self._lock:
            self._tokens = -seconds * self.rate


class HttpClient:
    """Un client par source : chaque source a sa propre politique de débit."""

    def __init__(
        self,
        name: str,
        rate: float,
        burst: int = 1,
        base_url: str = "",
        headers: dict[str, str] | None = None,
        cache_dir: Path | None = None,
    ):
        cfg = get_settings()
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.bucket = TokenBucket(rate, burst)
        self.timeout = cfg.http_timeout
        self.max_retries = cfg.http_max_retries
        self.backoff_base = cfg.http_backoff_base
        self.backoff_max = cfg.http_backoff_max
        self.cache_dir = cache_dir
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": cfg.user_agent, "Accept-Encoding": "gzip"})
        if headers:
            self.session.headers.update(headers)

    # ------------------------------------------------------------------ core
    def _url(self, path_or_url: str) -> str:
        if path_or_url.startswith("http"):
            return path_or_url
        return f"{self.base_url}/{path_or_url.lstrip('/')}"

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = self.backoff_base ** attempt
        else:
            delay = self.backoff_base ** attempt
        delay = min(self.backoff_max, delay) * random.uniform(0.8, 1.2)  # jitter
        log.warning("[%s] nouvel essai dans %.1fs (tentative %d/%d)",
                    self.name, delay, attempt, self.max_retries)
        time.sleep(delay)

    def request(self, method: str, path_or_url: str, **kwargs: Any) -> requests.Response:
        url = self._url(path_or_url)
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self.bucket.acquire()
            try:
                resp = self.session.request(method, url, **kwargs)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                last_exc = exc
                log.warning("[%s] erreur réseau sur %s : %s", self.name, url, exc)
                self._sleep_backoff(attempt, None)
                continue

            if resp.status_code == 304 or resp.ok:
                return resp
            if resp.status_code == 404:
                raise NotFound(f"404 sur {url}", 404, url)
            if resp.status_code in RETRYABLE_STATUS:
                retry_after = resp.headers.get("Retry-After")
                if resp.status_code == 429:
                    self.bucket.penalize(float(retry_after or 10))
                last_exc = HttpError(f"HTTP {resp.status_code}", resp.status_code, url)
                self._sleep_backoff(attempt, retry_after)
                continue
            # 4xx non récupérable (401, 403…) : inutile d'insister
            raise HttpError(f"HTTP {resp.status_code} sur {url} : {resp.text[:200]}", resp.status_code, url)

        raise HttpError(f"[{self.name}] échec après {self.max_retries} tentatives : {url}") from last_exc

    # --------------------------------------------------------------- helpers
    def get_json(self, path_or_url: str, params: dict | None = None) -> Any:
        return self.request("GET", path_or_url, params=params).json()

    def get_bytes(self, path_or_url: str, params: dict | None = None) -> bytes:
        return self.request("GET", path_or_url, params=params).content

    def download(self, path_or_url: str, dest: Path, force: bool = False) -> tuple[Path, bool]:
        """Télécharge vers `dest` avec requête conditionnelle (ETag / Last-Modified).

        Retourne (chemin, modifié?). Si le serveur répond 304, le fichier local est conservé.
        """
        dest.parent.mkdir(parents=True, exist_ok=True)
        meta_path = dest.with_suffix(dest.suffix + ".meta.json")
        headers: dict[str, str] = {}
        if dest.exists() and meta_path.exists() and not force:
            meta = json.loads(meta_path.read_text())
            if meta.get("etag"):
                headers["If-None-Match"] = meta["etag"]
            if meta.get("last_modified"):
                headers["If-Modified-Since"] = meta["last_modified"]

        resp = self.request("GET", path_or_url, headers=headers)
        if resp.status_code == 304:
            log.info("[%s] inchangé (304) : %s", self.name, dest.name)
            return dest, False

        content = resp.content
        new_hash = hashlib.md5(content).hexdigest()
        old_hash = hashlib.md5(dest.read_bytes()).hexdigest() if dest.exists() else None
        tmp = dest.with_suffix(dest.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(dest)  # écriture atomique
        meta_path.write_text(json.dumps({
            "url": resp.url,
            "etag": resp.headers.get("ETag"),
            "last_modified": resp.headers.get("Last-Modified"),
            "md5": new_hash,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }, indent=2))
        return dest, new_hash != old_hash

    def paginate(
        self,
        path: str,
        params: dict | None = None,
        page_param: str = "page",
        results_key: str = "response",
        total_pages_getter=lambda payload: payload.get("paging", {}).get("total", 1),
    ) -> Iterator[Any]:
        """Pagination par numéro de page (convention API-Football : paging.current/total)."""
        params = dict(params or {})
        page = 1
        while True:
            params[page_param] = page
            payload = self.get_json(path, params=params)
            yield from payload.get(results_key, [])
            if page >= int(total_pages_getter(payload) or 1):
                break
            page += 1


def client_for(source: str, **overrides: Any) -> HttpClient:
    """Fabrique un client à partir de config/sources.yaml."""
    src = get_settings().sources[source]
    rl = src.get("rate_limit", {"rate": 1.0, "burst": 1})
    return HttpClient(
        name=source,
        rate=rl["rate"],
        burst=rl.get("burst", 1),
        base_url=src.get("base_url", ""),
        **overrides,
    )
