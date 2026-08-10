"""
searxng_client.py
SearXNG 冗長構成クライアント【v11.0 CLA-247対応】

メイン：n8n SearXNG Proxy WF (06oeAsMwCSXGpNK3) → Oracle VM (localhost:8082)
サブ  ：Render (searxng-main.onrender.com)

変更点 (CLA-247):
  Oracle IP直叩き(161.33.140.166:8082)を廃止
  → n8n SearXNG Proxy WF経由に変更（Cloud RunからOracle到達不能問題を解消）
"""

import asyncio
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ===== SearXNG エンドポイント設定 =====
# メイン: n8n SearXNG Proxy WF が Oracle VM の localhost:8082 に中継する
#         WF ID: 06oeAsMwCSXGpNK3
# サブ:   Render（Cloud Runから直接アクセス可能・スリープ防止WF稼働中）
SEARXNG_ENDPOINTS = [
    {
        "name": "n8n Proxy (Oracle)",
        "url": "https://omoikane-1.tail32db64.ts.net/webhook/e37ad53d-0536-44da-9bb1-b1af972c6b2f/searxng-proxy",
        "append_search_path": False,
        "priority": 1,
    },
    {
        "name": "Render",
        "url": "https://searxng-main.onrender.com",
        "append_search_path": True,
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
    if endpoint.get("append_search_path", True):
        url = f"{endpoint['url']}/search"
    else:
        url = endpoint["url"]
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
        logger.warning("[SearXNG] %s タイムアだト", endpoint["name"])
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
