"""Response helpers for the ai-goods client."""

import re
from pathlib import Path
from typing import Any

import httpx

from aigoods_worker.models import AigoodsError


def _json(resp: httpx.Response) -> dict[str, Any]:
    try:
        data = resp.json()
        return data if isinstance(data, dict) else {"payload": data}
    except ValueError:
        return {}


def _checked(resp: httpx.Response) -> dict[str, Any]:
    data = _json(resp)
    if resp.status_code != 200 or data.get("status") == "ERROR":
        raise AigoodsError(f"{resp.request.url.path}: HTTP {resp.status_code} {data.get('message') or data.get('errors')}")
    return data


def _filename(resp: httpx.Response) -> str | None:
    m = re.search(r'filename[^;=\n]*=([\'"]?)([^;\n\'"]+)\1', resp.headers.get("content-disposition", ""))
    return Path(m.group(2)).name if m else None
