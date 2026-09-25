"""Data types and errors for the ai-goods client."""

from dataclasses import dataclass


class AigoodsError(Exception):
    """API returned an error status or unexpected payload."""


class AuthError(AigoodsError):
    """Login failed or session expired and re-login did not help."""


@dataclass(frozen=True)
class Billing:
    """Cardholder contact details required by /deposit/validate and /deposit/checkout."""

    name: str
    surname: str
    email: str
    phone: str
    country_id: int
    city: str
    address: str
    post_code: str
