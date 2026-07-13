"""
main.py - FastAPI メインエントリーポイント【v8.01】

【アーキテクチャ】
  ユーザー → Cloud Run（このファイル）
              ├─ Yahoo API（並列）
              ├─ 楽天 API（並列）
              ├─ SearXNG → Amazon価格取得
              └─ ヨドバシ 検索URLリンク生成

【処理フロー】
  1. Yahoo/楽天 API を asyncio.gather で並列取得
  2. regex_parser で容量・入数を正規化
  3. ai_parser（Ollama）で補完（regex失敗分のみ）
  4. calculator で単価計算
  5. sorter でソート
  6. affiliate_recomposer でアフィリエイトURL合成
  7. SearXNG で Amazon価格取得（並列）
  8. ヨドバシ 検索URLを生成して追加
  9. レスポンス返却
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Optional

import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from schemas import (
    MallType, RawItem, ParsedItem,
    AffiliateItem, affiliate_item_to_dict,
)
from regex_parser import parse_items_with_regex
from ai_parser import parse_items_with_ai
from calculator import calculate_all
from sorter import sort_by_unit_price
from affiliate_recomposer import recompose_affiliate_urls
from yahoo_api import fetch_yahoo_items
from rakuten_api import fetch_rakuten_items
from searxng_client import search_with_fallback

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Price Ranking API", version="8.01")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────

AMAZON_AFFILIATE_TAG = "ggvssyakumo-22"

# SearXNG エンドポイント（Oracle VM / Koyeb フォールバック）
SEARXNG_ENDPOINTS = [
    "http://161.33.140.166:8080",   # Oracle VM（メイン）
    "https://civic-marilin-ggvss-a16849cf.koyeb.app",  # Koyeb（サブ）
]

# ──────────────────────────────────────────────
# Amazon価格抽出（SearXNG経由）
# ──────────────────────────────────────────────

_PRICE_RE = re.compile(r"[¥￥][\s]?([\d,]+)")


def _extract_price_from_snippet(content: str) -> Optional[int]:
    """
    SearXNG のスニペット文字列から価格を抽出する。

    例: "¥1,980 送料無料" → 1980
    """
    m = _PRICE_RE.search(content)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


async def fetch_amazon_price_via_searxng(
    query: str,
    capacity_ml: Optional[float],
    quantity: int,
) -> Optional[dict[str, Any]]:
    """
    SearXNG を使って Amazon の価格をスニペットから取得する。

    Step 2-6 実装：
      1. 容量・入数情報を付加した検索クエリを生成
      2. Amazon 検索 URL を SearXNG に投げる
      3. スニペットから /[¥￥][\s]?([\d,]+)/ で価格抽出
      4. アフィリエイトタグ付き Amazon 検索 URL を生成して返す

    Returns:
        {
            "price": int,           # 抽出した価格
            "affiliate_url": str,   # Amazon検索URL（アフィリタグ付き）
            "mall": "amazon",
        }
        or None（価格取得失敗時）
    """
    # 検索クエリ生成（容量・入数を付加して精度向上）
    parts = [query]
    if capacity_ml is not None:
        if capacity_ml >= 1000 and capacity_ml % 1000 == 0:
            parts.append(f"{int(capacity_ml // 1000)}L")
        else:
            parts.append(f"{int(capacity_ml)}ml")
    if quantity > 1:
        parts.append(f"{quantity}本")

    amazon_query = " ".join(parts)
    amazon_search_url = (
        f"https://www.amazon.co.jp/s?k={amazon_query}"
        f"&tag={AMAZON_AFFILIATE_TAG}"
    )

    # SearXNG に投げる
    result = await search_with_fallback(
        query=amazon_search_url,
        params={},  # エンジン指定なし＝全エンジン（地雷集より）
    )

    if not result:
        logger.warning("SearXNG: 全エンドポイント失敗 query='%s'", amazon_query)
        return None

    # スニペットから価格抽出（上位5件を探索）
    items = result.get("data", {}).get("results", [])
    for item in items[:5]:
        snippet = item.get("content", "") or item.get("title", "")
        price = _extract_price_from_snippet(snippet)
        if price and price > 0:
            logger.info("SearXNG Amazon価格取得成功: %d円 / query='%s'", price, amazon_query)
            return {
                "price": price,
                "affiliate_url": amazon_search_url,
                "mall": "amazon",
                "raw_name": f"Amazon: {amazon_query}",
            }

    logger.warning("SearXNG: スニペットから価格を抽出できず query='%s'", amazon_query)
    return None


# ──────────────────────────────────────────────
# ヨドバシ検索URLリンク生成
# ──────────────────────────────────────────────

def build_yodobashi_result(query: str) -> dict[str, Any]:
    """
    Step 2-6a：ヨドバシ検索URLを生成する。
    価格はリアルタイム取得不可のため「要確認」バッジを付ける。
    """
    from urllib.parse import quote
    encoded = quote(query)
    url = f"https://www.yodobashi.com/?word={encoded}"
    return {
        "mall": "yodobashi",
        "price": None,
        "affiliate_url": url,
        "raw_name": f"ヨドバシ: {query}（価格は要確認）",
        "price_unconfirmed": True,  # フロント側で「要確認」バッジ表示用
    }


# ──────────────────────────────────────────────
# メイン検索エンドポイント
# ──────────────────────────────────────────────

@app.get("/search")
async def search(q: str, limit: int = 30) -> JSONResponse:
    """
    商品名でYahoo・楽天・Amazon（SearXNG）・ヨドバシを横断検索して
    単価順にソートした結果を返す。

    Args:
        q     : 検索キーワード（例: コカコーラ 350ml）
        limit : 取得件数上限（デフォルト30）

    Returns:
        {
            "query": str,
            "items": [...],         # Yahoo・楽天の結果（単価順）
            "amazon": {...} | null, # SearXNG経由のAmazon結果
            "yodobashi": {...},     # ヨドバシリンク（要確認）
            "total": int,
        }
    """
    if not q or not q.strip():
        return JSONResponse({"error": "クエリが空です"}, status_code=400)

    query = q.strip()
    logger.info("検索開始: '%s' limit=%d", query, limit)

    # ── Step 1: Yahoo / 楽天 を並列取得 ──
    try:
        yahoo_items, rakuten_items = await asyncio.gather(
            fetch_yahoo_items(query, limit=limit),
            fetch_rakuten_items(query, limit=limit),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.error("API並列取得エラー: %s", exc)
        yahoo_items, rakuten_items = [], []

    # gather が例外を返した場合は空リストに置き換え
    if isinstance(yahoo_items, Exception):
        logger.error("Yahoo API エラー: %s", yahoo_items)
        yahoo_items = []
    if isinstance(rakuten_items, Exception):
        logger.error("楽天 API エラー: %s", rakuten_items)
        rakuten_items = []

    raw_items: list[RawItem] = list(yahoo_items) + list(rakuten_items)
    logger.info("取得完了: Yahoo=%d件, 楽天=%d件", len(yahoo_items), len(rakuten_items))

    # ── Step 2: 容量・入数 正規化（regex_parser）──
    parsed_items = parse_items_with_regex(raw_items)

    # ── Step 3: Ollama で補完（regex失敗分のみ・エコ設計）──
    parsed_items = await parse_items_with_ai(parsed_items)

    # ── Step 4: 単価計算 ──
    priced_items = calculate_all(parsed_items)

    # ── Step 5: 単価順ソート ──
    sorted_items = sort_by_unit_price(priced_items)

    # ── Step 6: アフィリエイトURL合成 ──
    affiliate_items = recompose_affiliate_urls(sorted_items)

    # ── Step 7: SearXNG → Amazon価格取得（並列・非同期）──
    # 最もマッチしそうなアイテムの容量・入数を使って精度向上
    best_capacity = next(
        (i.capacity_ml for i in parsed_items if i.capacity_ml is not None), None
    )
    best_quantity = next(
        (i.quantity for i in parsed_items if i.quantity > 1), 1
    )

    amazon_result = await fetch_amazon_price_via_searxng(
        query=query,
        capacity_ml=best_capacity,
        quantity=best_quantity,
    )

    # ── Step 8: ヨドバシリンク生成 ──
    yodobashi_result = build_yodobashi_result(query)

    # ── Step 9: レスポンス組み立て ──
    return JSONResponse({
        "query": query,
        "items": [affiliate_item_to_dict(item) for item in affiliate_items],
        "amazon": amazon_result,
        "yodobashi": yodobashi_result,
        "total": len(affiliate_items),
        "meta": {
            "yahoo_count":   len(yahoo_items),
            "rakuten_count": len(rakuten_items),
            "amazon_found":  amazon_result is not None,
        },
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "version": "8.01"})
