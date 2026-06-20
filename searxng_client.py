"""searxng_client.py
SearXNG 冗長構成クライアント

メイン：HuggingFace (PRDocker2)
サブ  ：Koyeb
将来  ：Oracle A1（取得後に追加予定）
"""

import asyncio
import aiohttp
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ===== SearXNG エンドポイント設定 =====
SEARXNG_ENDPOINTS = [
    {
        "name": "HuggingFace",
        "url": "https://ggvssyakumo01-prdocker2.hf.space",
        "priority": 1,
    },
    {
        "name": "Koyeb",
        "url": "https://civic-marilin-ggvss-a16849cf.koyeb.app",
        "priority": 2,
    },
    # 将来：Oracle A1 取得後にここへ追加
    # {
    #     "name": "Oracle",
    #     "url": "https://YOUR_ORACLE_IP/searxng",
    #     "priority": 3,
    # },
]

TIMEOUT_SEC = 8  # 各エンドポイントのタイムアウト
PING_INTERVAL_MIN = 25  # BAN回避 ping 間隔（分）


async def _fetch_searxng(session: aiohttp.ClientSession, endpoint: dict, query: str, params: dict) -> Optional[dict]:
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
                logger.info(f"[SearXNG] {endpoint['name']} 成功")
                return {"source": endpoint["name"], "data": data}
            else:
                logger.warning(f"[SearXNG] {endpoint['name']} HTTP {resp.status}")
                return None
    except asyncio.TimeoutError:
        logger.warning(f"[SearXNG] {endpoint['name']} タイムアウト")
        return None
    except Exception as e:
        logger.warning(f"[SearXNG] {endpoint['name']} エラー: {e}")
        return None


async def search_with_fallback(query: str, params: dict = {}) -> Optional[dict]:
    """
    優先順位に従ってSearXNGを叩く。
    メインが失敗したらサブに自動フォールバック。
    Promise.any()方式：最初に成功したものを返す。
    """
    async with aiohttp.ClientSession() as session:
        # 優先順位順にソート
        endpoints = sorted(SEARXNG_ENDPOINTS, key=lambda x: x["priority"])

        # まずメインを試す
        for endpoint in endpoints:
            result = await _fetch_searxng(session, endpoint, query, params)
            if result is not None:
                return result

        logger.error("[SearXNG] 全エンドポイント失敗")
        return None


async def search_all_parallel(query: str, params: dict = {}) -> list:
    """
    全エンドポイントに並列リクエストして結果をマージ。
    （SearXNG マージ処理 Step 2-6 用）
    """
    async with aiohttp.ClientSession() as session:
        tasks = [
            _fetch_searxng(session, ep, query, params)
            for ep in SEARXNG_ENDPOINTS
        ]
        results = await asyncio.gather(*tasks)
        return [r for r in results if r is not None]


def search_sync(query: str, params: dict = {}) -> Optional[dict]:
    """同期ラッパー（Flask から呼び出す用）"""
    return asyncio.run(search_with_fallback(query, params))


def search_all_sync(query: str, params: dict = {}) -> list:
    """並列検索の同期ラッパー（Flask から呼び出す用）"""
    return asyncio.run(search_all_parallel(query, params))
