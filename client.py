"""
Async client for ai-goods.eu (Laravel Sanctum SPA API).

Auth flow: GET /sanctum/csrf-cookie → POST /api/login {username, password}.
Every state-changing request must carry X-XSRF-TOKEN (url-decoded XSRF-TOKEN cookie).
Session cookies are persisted to disk so the bot does not re-login on every start.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from aigoods_worker.config import AigoodsSettings
from aigoods_worker.http_utils import _checked, _filename, _json
from aigoods_worker.models import AigoodsError, AuthError, Billing


_network_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=2, max=20),
    reraise=True,
)


class AigoodsClient:
    """Thin async wrapper over the ai-goods.eu API used by the site frontend."""

    def __init__(self, settings: AigoodsSettings) -> None:
        self.s = settings
        self._cookies_path = Path(settings.cookies_path)
        self._http = httpx.AsyncClient(
            base_url=settings.api_base,
            proxy=settings.proxy_url,
            timeout=settings.request_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "application/json",
                "Origin": settings.site_origin,
                "Referer": f"{settings.site_origin}/",
                "frontend-language": "en",
            },
        )
        self._load_cookies()

    async def __aenter__(self) -> "AigoodsClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Persist cookies and close the HTTP connection pool."""
        self._save_cookies()
        await self._http.aclose()

    # ------------------------------------------------------------------ auth

    async def login(self) -> None:
        """Fresh login; raises AuthError on bad credentials."""
        await self._http.get("/sanctum/csrf-cookie")
        resp = await self._http.post(
            "/api/login",
            json={"username": self.s.email, "password": self.s.password},
            headers=self._xsrf(),
        )
        data = _json(resp)
        if resp.status_code != 200 or data.get("status") != "OK":
            raise AuthError(f"login failed: HTTP {resp.status_code} {data.get('message')}")
        self._save_cookies()
        logger.info("ai-goods: logged in")

    async def ensure_logged_in(self) -> None:
        """Re-use the saved session if still valid, otherwise log in again."""
        resp = await self._http.get("/api/user/profile")
        if resp.status_code == 200:
            return
        logger.info(f"ai-goods: session invalid (HTTP {resp.status_code}), re-login")
        await self.login()

    # --------------------------------------------------------------- reading

    async def get_settings(self) -> dict[str, Any]:
        """Public site settings: generation cost, min deposit, formats, sizes."""
        return (await self._get("/api/settings"))["payload"]

    async def get_profile(self) -> dict[str, Any]:
        """Current user profile including balance_eur."""
        return (await self._get("/api/user/profile"))["payload"]

    async def get_balance_eur(self) -> float:
        """Account balance in EUR."""
        return float((await self.get_profile())["balance_eur"])

    async def get_deposit_history(self) -> list[dict[str, Any]]:
        """All deposits, newest first."""
        return (await self._get("/api/deposit/history"))["payload"]

    async def get_purchase_history(self) -> list[dict[str, Any]]:
        """Purchases and generations."""
        data = await self._get("/api/purchase/history")
        return data.get("payload", data.get("data", []))

    # -------------------------------------------------------------- deposits

    async def create_deposit(self, billing: Billing, amount_eur: float) -> str:
        """Create a card deposit and return the payment-gateway checkout URL."""
        body = {
            "name": billing.name,
            "surname": billing.surname,
            "email": billing.email,
            "phone": billing.phone,
            "country": billing.country_id,
            "currency": "EUR",
            "city": billing.city,
            "address": billing.address,
            "postCode": billing.post_code,
            "amount": amount_eur,
        }
        await self._post("/api/deposit/validate-amount", {"amount": amount_eur, "currency": "EUR"})
        await self._post("/api/deposit/validate", body)
        data = await self._post(
            "/api/deposit/checkout", {**body, "paymentType": "card", "payment_method_id": 1}
        )
        url = data.get("redirect_url")
        if not url:
            raise AigoodsError(f"checkout returned no redirect_url: {data}")
        logger.info(f"ai-goods: deposit €{amount_eur} created")
        return url

    # ------------------------------------------------------------ generation

    async def generate_image(
        self, prompt: str, title: str = "", fmt: str = "png", size: str = "1024x1024"
    ) -> Path:
        """Generate one image (costs image_generation_cost_eur). Returns path to the zip archive."""
        await self.ensure_logged_in()
        resp = await self._http.post(
            "/api/purchase/generate-image",
            json={"title": title or prompt[:60], "prompt": prompt, "format": fmt, "size": size},
            headers=self._xsrf(),
            timeout=self.s.generation_timeout,
        )
        if resp.headers.get("content-type", "").startswith("application/zip"):
            name = _filename(resp) or f"gen_{datetime.now():%Y%m%d_%H%M%S}.zip"
            path = self.s.downloads_path / name
            path.write_bytes(resp.content)
            logger.info(f"ai-goods: generated → {path}")
            return path
        raise AigoodsError(f"generation failed: HTTP {resp.status_code} {_json(resp).get('message')}")

    # ------------------------------------------------------------- internals

    @_network_retry
    async def _get(self, path: str) -> dict[str, Any]:
        resp = await self._http.get(path)
        if resp.status_code == 401:
            await self.login()
            resp = await self._http.get(path)
        return _checked(resp)

    @_network_retry
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post(path, json=body, headers=self._xsrf())
        if resp.status_code in (401, 419):  # 419 = CSRF token expired
            await self.login()
            resp = await self._http.post(path, json=body, headers=self._xsrf())
        return _checked(resp)

    def _xsrf(self) -> dict[str, str]:
        token = self._http.cookies.get("XSRF-TOKEN")
        return {"X-XSRF-TOKEN": unquote(token)} if token else {}

    def _load_cookies(self) -> None:
        if not self._cookies_path.exists():
            return
        try:
            for c in json.loads(self._cookies_path.read_text()):
                self._http.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
        except (ValueError, KeyError) as e:
            logger.warning(f"ai-goods: ignoring broken cookies file: {e}")

    def _save_cookies(self) -> None:
        cookies = [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in self._http.cookies.jar
        ]
        self._cookies_path.parent.mkdir(parents=True, exist_ok=True)
        self._cookies_path.write_text(json.dumps(cookies))
        self._cookies_path.chmod(0o600)
