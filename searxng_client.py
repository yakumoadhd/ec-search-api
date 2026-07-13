"""
searxng_client.py
SearXNG 冗長構成クライアント【v8.01更新】

メイン：Oracle VM (161.33.140.166:8080)
サブ  ：Koyeb
"""

import asyncio
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ===== SearXNG エンドポイント設定 =====
SEARXNG_ENDPOINTS = [
    {
        "name": "Oracle",
        "url": "http://161.33.140.166:8080",
        "priority": 1,
    },
    {
        "name": "Koyeb",
        "url": "https://civic-marilin-ggvss-a16849cf.koyeb.app",
        "priority": 2,
    },
]

TIMEOUT_SEC = 8


async def _fetch_searxng(
    session: aiohttp.ClientSession,
    endpoint: dict,
    query: str,
    params: dict,
) -> Optional[dict]:
    """単一エンドポイントへリクエスト。失敗時はNoneを返す。"""
    url = f"{endpoint['url']}/search"
    try:
        async with session.get(
            url,
            params={"q": query, "format": "json", **params},
            timeout=aiohttp.ClientTimeout(total=TIMEOUT_SEC),
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                logger.info("[SearXNG] %s 成功", endpoint["name"])
                return {"source": endpoint["name"], "data": data}
            else:
                logger.warning("[SearXNG] %s HTTP %d", endpoint["name"], resp.status)
                return None
    except asyncio.TimeoutError:
        logger.warning("[SearXNG] %s タイムアウト", endpoint["name"])
        return None
    except Exception as e:
        logger.warning("[SearXNG] %s エラー: %s", endpoint["name"], e)
        return None


async def search_with_fallback(
    query: str,
    params: dict = {},
) -> Optional[dict]:
    """
    優先順位に従ってSearXNGを叩く。
    メインが失敗したらサブに自動フォールバック。
    """
    async with aiohttp.ClientSession() as session:
        endpoints = sorted(SEARXNG_ENDPOINTS, key=lambda x: x["priority"])
        for endpoint in endpoints:
            result = await _fetch_searxng(session, endpoint, query, params)
            if result is not None:
                return result

    logger.error("[SearXNG] 全エンドポイント失敗")
    return None


async def search_all_parallel(
    query: str,
    params: dict = {},
) -> list:
    """全エンドポイントに並列リクエストして結果をマージ。"""
    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_searxng(session, ep, query, params)
            for ep in SEARXNG_ENDPOINTS
        ]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]
