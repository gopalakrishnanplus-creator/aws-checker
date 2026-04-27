from __future__ import annotations

from urllib.parse import urljoin

import requests
from django.conf import settings


class PJTIntegrationClient:
    def is_configured(self) -> bool:
        return bool(settings.PJT_INTEGRATION_BASE_URL and settings.PJT_INTEGRATION_BEARER_TOKEN)

    def assert_configured(self):
        if not self.is_configured():
            raise ValueError(
                "PJT integration is not configured. Set PJT_INTEGRATION_BASE_URL and "
                "PJT_INTEGRATION_BEARER_TOKEN in the runtime environment."
            )

    def build_url(self, *, path: str | None = None, url: str | None = None) -> str:
        if url:
            return url
        self.assert_configured()
        if not path:
            path = settings.PJT_INTEGRATION_TRIGGER_PATH
        base = settings.PJT_INTEGRATION_BASE_URL.rstrip("/") + "/"
        return urljoin(base, path.lstrip("/"))

    def build_headers(self, headers: dict | None = None) -> dict:
        self.assert_configured()
        merged = dict(headers or {})
        merged.setdefault("Authorization", f"Bearer {settings.PJT_INTEGRATION_BEARER_TOKEN}")
        merged.setdefault("X-Contract-Version", settings.PJT_INTEGRATION_CONTRACT_VERSION)
        return merged

    def request(
        self,
        *,
        method: str = "POST",
        path: str | None = None,
        url: str | None = None,
        headers: dict | None = None,
        timeout: int | float | None = None,
        **kwargs,
    ):
        target_url = self.build_url(path=path, url=url)
        final_headers = self.build_headers(headers=headers)
        return requests.request(
            method=method.upper(),
            url=target_url,
            headers=final_headers,
            timeout=timeout or settings.AWS_CHECKER_HEALTHCHECK_TIMEOUT,
            **kwargs,
        )

    def trigger_run(self, payload: dict | None = None):
        return self.request(
            method="POST",
            path=settings.PJT_INTEGRATION_TRIGGER_PATH,
            json=payload or {},
        )
