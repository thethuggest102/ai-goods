from pathlib import Path
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict


class AigoodsSettings(BaseSettings):
    """Settings for the ai-goods.eu worker (env prefix AIGOODS_)."""

    model_config = SettingsConfigDict(
        env_prefix="AIGOODS_",
        env_file=(".env", "data/aigoods.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    email: str
    password: str
    proxy: str = ""  # format: host:port:user:pass — keep ONE fixed proxy per account

    api_base: str = "https://api.ai-goods.eu"
    site_origin: str = "https://ai-goods.eu"
    cookies_path: str = "./data/aigoods_cookies.json"
    downloads_dir: str = "./data/aigoods"
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    )
    headless: bool = False  # visible browser locally; on the server runs under Xvfb
    admin_host: str = "127.0.0.1"
    admin_port: int = 8090
    admin_password: str = ""  # HTTP Basic auth for the admin page (required on the server)
    link_ingest_token: str = ""  # shared secret; the external admin sends checkout links with it
    checkout_host: str = "checkout.kingoppay.com"  # only accept payment links on this host

    # Billing contact details sent to ai-goods with every deposit (Kaspi → Kazakhstan)
    billing_name: str = ""
    billing_surname: str = ""
    billing_phone: str = ""
    billing_country_id: int = 112  # Kazakhstan in /api/countries
    billing_city: str = ""
    billing_address: str = ""
    billing_post_code: str = ""
    request_timeout: float = 60.0
    generation_timeout: float = 300.0

    @property
    def proxy_url(self) -> str | None:
        """Convert host:port:user:pass to http://user:pass@host:port."""
        if not self.proxy:
            return None
        parts = self.proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            return f"http://{quote(user, safe='')}:{quote(pwd, safe='')}@{host}:{port}"
        if len(parts) == 2:
            return f"http://{parts[0]}:{parts[1]}"
        return None

    @property
    def downloads_path(self) -> Path:
        p = Path(self.downloads_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p
