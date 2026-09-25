"""
Local admin page for topping up ai-goods balance with a card typed by the owner.

    .venv/bin/python -m aigoods_worker.admin   → http://127.0.0.1:8090

Card data goes: browser form → this process memory → gateway form in the bot browser.
It is never written to disk, DB or logs.
"""

import asyncio
import base64
import hmac
import sys
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import web
from loguru import logger

from aigoods_worker.client import AigoodsClient
from aigoods_worker.models import Billing
from aigoods_worker.config import AigoodsSettings
from aigoods_worker.payment import Card
from aigoods_worker.topup import TopupJob, run_topup

settings = AigoodsSettings()
_job: TopupJob | None = None
_pending_link: dict[str, object] | None = None  # {url, amount} pushed by the external admin
_tasks: set[asyncio.Task[None]] = set()
_PAGE = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")

# The link-intake webhook uses its own shared-secret token, not the admin's Basic auth.
_TOKEN_PATH = "/api/checkout-link"


@web.middleware
async def basic_auth(request: web.Request, handler):  # type: ignore[no-untyped-def]
    """Require HTTP Basic auth when AIGOODS_ADMIN_PASSWORD is set (except the token webhook)."""
    if settings.admin_password and request.path != _TOKEN_PATH:
        expected = "Basic " + base64.b64encode(f"admin:{settings.admin_password}".encode()).decode()
        if request.headers.get("Authorization") != expected:
            raise web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="admin"'})
    return await handler(request)


async def index(_: web.Request) -> web.Response:
    """Serve the admin page."""
    return web.Response(text=_PAGE, content_type="text/html")


async def info(_: web.Request) -> web.Response:
    """Balance and billing defaults for the form."""
    async with AigoodsClient(settings) as c:
        await c.ensure_logged_in()
        balance = await c.get_balance_eur()
    s = settings
    return web.json_response({
        "balance": balance,
        "billing": {"name": s.billing_name, "surname": s.billing_surname, "phone": s.billing_phone,
                    "city": s.billing_city, "address": s.billing_address, "post_code": s.billing_post_code},
    })


async def checkout_link(request: web.Request) -> web.Response:
    """Webhook: the external admin pushes a kingoppay checkout link to pay (URL only, never a card)."""
    global _pending_link
    token = request.headers.get("X-Auth-Token", "")
    if not settings.link_ingest_token or not hmac.compare_digest(token, settings.link_ingest_token):
        raise web.HTTPUnauthorized(text="bad token")
    d = await request.json()
    url = str(d.get("url", ""))
    if urlsplit(url).hostname != settings.checkout_host:
        return web.json_response({"error": f"URL must be on {settings.checkout_host}"}, status=400)
    _pending_link = {"url": url, "amount": float(d["amount"]) if d.get("amount") else None}
    logger.info(f"checkout link received (amount={_pending_link['amount']})")
    return web.json_response({"ok": True})


async def pending(_: web.Request) -> web.Response:
    """The checkout link waiting to be paid, if any (shown on the admin page)."""
    if _pending_link is None:
        return web.json_response({"pending": False})
    return web.json_response({"pending": True, "amount": _pending_link["amount"]})


async def topup(request: web.Request) -> web.Response:
    """Start a top-up; only one at a time. Uses a pushed checkout link if one is waiting."""
    global _job, _pending_link
    if _job is not None and not _job.done:
        return web.json_response({"error": "Пополнение уже идёт"}, status=409)
    d = await request.json()
    link = _pending_link
    amount = float(link["amount"]) if link and link.get("amount") else float(d["amount"])
    if amount < 5:
        return web.json_response({"error": "Минимум €5"}, status=400)
    card = Card(number=d["card_number"], expiry=d["card_expiry"], cvc=d["card_cvc"], holder=d["card_holder"])
    billing = Billing(
        name=d["name"], surname=d["surname"], email=settings.email, phone=d["phone"],
        country_id=settings.billing_country_id, city=d["city"], address=d["address"], post_code=d["post_code"],
    )
    del d
    checkout_url = str(link["url"]) if link else None
    _pending_link = None
    _job = TopupJob(amount_eur=amount)
    task = asyncio.create_task(run_topup(settings, billing, card, _job, checkout_url))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return web.json_response({"started": True})


async def status(_: web.Request) -> web.Response:
    """Current top-up state for polling."""
    if _job is None:
        return web.json_response({"idle": True})
    j = _job
    return web.json_response({"status": j.status, "done": j.done, "ok": j.ok, "needs_otp": j.needs_otp,
                              "balance_after": j.balance_after})


async def otp(request: web.Request) -> web.Response:
    """Receive SMS 3-D Secure code from the owner."""
    if _job is not None:
        _job.submit_otp(str((await request.json())["code"]).strip())
    return web.json_response({"ok": True})


def main() -> None:
    """Run the admin server."""
    logger.remove()
    logger.add(sys.stderr, level="INFO", diagnose=False, backtrace=False)  # no variable values in tracebacks
    app = web.Application(middlewares=[basic_auth])
    app.add_routes([web.get("/", index), web.get("/api/info", info), web.post("/api/topup", topup),
                    web.get("/api/status", status), web.post("/api/otp", otp),
                    web.get("/api/pending", pending), web.post(_TOKEN_PATH, checkout_link)])
    web.run_app(app, host=settings.admin_host, port=settings.admin_port, access_log=None)


if __name__ == "__main__":
    main()
