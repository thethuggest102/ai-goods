"""Top-up orchestration: create deposit on ai-goods → pay in browser → confirm balance."""

import asyncio
from dataclasses import dataclass, field

from loguru import logger

from aigoods_worker.client import AigoodsClient
from aigoods_worker.models import Billing
from aigoods_worker.config import AigoodsSettings
from aigoods_worker.payment import Card, PayResult, pay_checkout


@dataclass
class TopupJob:
    """State of one top-up, polled by the admin page."""

    amount_eur: float
    status: str = "Создаю пополнение на ai-goods"
    done: bool = False
    ok: bool = False
    needs_otp: bool = False
    balance_before: float = 0.0
    balance_after: float | None = None
    _otp: asyncio.Future[str] | None = field(default=None, repr=False)

    def set_status(self, text: str) -> None:
        self.status = text
        logger.info(f"topup €{self.amount_eur}: {text}")

    async def ask_otp(self) -> str:
        self._otp = asyncio.get_running_loop().create_future()
        self.needs_otp = True
        try:
            return await asyncio.wait_for(self._otp, timeout=240)
        finally:
            self.needs_otp = False

    def submit_otp(self, code: str) -> None:
        if self._otp is not None and not self._otp.done():
            self._otp.set_result(code)


async def run_topup(
    settings: AigoodsSettings,
    billing: Billing,
    card: Card,
    job: TopupJob,
    checkout_url: str | None = None,
) -> None:
    """Full top-up. Card object is dropped as soon as the browser step ends.

    If checkout_url is given (provided by the external admin), pay that link directly;
    otherwise the bot creates the deposit on ai-goods itself and pays the resulting link.
    """
    try:
        async with AigoodsClient(settings) as client:
            await client.ensure_logged_in()
            job.balance_before = await client.get_balance_eur()
            url = checkout_url or await client.create_deposit(billing, job.amount_eur)
            result = await pay_checkout(settings, url, card, job.ask_otp, job.set_status)
            del card
            if result is PayResult.FAILED:
                job.set_status("Банк или шлюз отклонил платёж")
                return
            job.set_status("Проверяю зачисление на баланс")
            for _ in range(30):
                job.balance_after = await client.get_balance_eur()
                if job.balance_after > job.balance_before:
                    job.ok = True
                    job.set_status(f"Готово: баланс €{job.balance_after:.2f}")
                    return
                await asyncio.sleep(10)
            job.set_status("Деньги пока не зачислены — проверьте историю пополнений позже")
    except Exception as e:
        logger.exception("topup failed")
        job.set_status(f"Ошибка: {type(e).__name__}: {e}")
    finally:
        job.done = True
