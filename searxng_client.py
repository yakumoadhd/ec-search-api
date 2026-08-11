import aiohttp
import logging

logger = logging.getLogger(__name__)

SEARXNG_PROXY_URL = "https://omoikane-1.tail32db64.ts.net/webhook/e37ad53d-0536-44da-9bb1-b1af972c6b2f/searxng-proxy"

async def search_products(keyword: str, timeout: int = 15) -> list:
    """n8n SearXNG Proxy経由で商品を検索する。
    Oracle VMのSearXNG(port:8082)カn8nがプロキシしてCORS問題を回避。
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                SEARXNG_PROXY_URL,
                params={"q": keyword},
                timeout=aiohttp.ClientTimeout(total=timeout)
            ) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
                results = data.get("results", [])
                logger.info(f"SearXNG proxy: {len(results)}件取得 keyword={keyword}")
                return results
    except aiohttp.ClientResponseError as e:
        logger.error(f"SearXNG proxy HTTP error: {e.status} {e.message}")
        return []
    except Exception as e:
        logger.error(f"SearXNG proxy error: {e}")
        return []
