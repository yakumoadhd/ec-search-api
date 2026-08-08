"""
amazon_api.py - Amazon Creators API クライアント（aiohttp版）

【設計方針】
- PA-API 5.0は2026年5月15日廃止済み → Creators APIに完全移行
- Creators APIは過去30日10件のqualifying salesが条件
  → 未達 / キー未設定 / エラーは全てNoneを返してSearXNGにフォールバックさせる
- fetch_amazon_result(query, capacity_ml, quantity) が公開インターフェース
  （main.pyのfetch_amazon_price_via_searxngと同一シグネチャ）
"""
from __future__ import annotations

import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────

_CREATORS_API_BASE = "https://affiliate.amazon.co.jp/creatorsapi"
_PARTNER_TAG       = "ggvssyakumo-22"
_REQUEST_TIMEOUT   = 10.0


# ──────────────────────────────────────────────
# 認証情報（環境変数から取得）
# ──────────────────────────────────────────────

def _is_configured() -> bool:
    """Creators API認証情報が環境変数に設定されているか確認する。"""
    return bool(
        os.environ.get("AMAZON_CREATORS_ACCESS_KEY", "").strip()
        and os.environ.get("AMAZON_CREATORS_SECRET_KEY", "").strip()
    )


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

async def fetch_amazon_result(
    query: str,
    capacity_ml: Optional[float] = None,
    quantity: int = 1,
) -> Optional[dict[str, Any]]:
    """
    Amazon Creators APIで商品を検索して価格情報を返す。

    main.pyのfetch_amazon_price_via_searxngと同一の戻り値形式:
        {
            "price":         int,   # 円
            "affiliate_url": str,   # アフィリエイトタグ付きURL
            "mall":          str,   # "amazon"
            "raw_name":      str,   # 商品名
        }

    以下の場合はNoneを返してSearXNGにフォールバックさせる:
        - オ認証情報未設定
        - qualifying sales 10件未達（API側が403/401を返す）
        - ネットワークエラー / タイムアウト
        - レスポンスに有効な商品がない

    【TODO: Creators API正式実装】
    qualifying sales 10件条件クリア後に以下を実装:
        - Associates Central > Product Advertising API > Creators API セクションで
          認証情報（ACCESS_KEY / SECRET_KEY）を発行
        - エンドポイント詳細: https://affiliate.amazon.co.jp/creatorsapi/docs/en-us/introduction
        - 署名方式: PA-API 5.0と同系統のAWS4-HMAC-SHA256（変更の可能性あり・要確認）
        - レート上限: 1リクエスト/秒から開始（売上実績で自動上昇）
    """
    if not _is_configured():
        logger.info("[Creators API] 認証情報未設定 → スキップ")
        return None

    # 検索クエリ生成（capacity/quantityを付加して精度向上）
    parts = [query]
    if capacity_ml is not None:
        if capacity_ml >= 1000 and capacity_ml % 1000 == 0:
            parts.append(f"{int(capacity_ml // 1000)}L")
        else:
            parts.append(f"{int(capacity_ml)}ml")
    if quantity > 1:
        parts.append(f"{quantity}本")
    refined_query = " ".join(parts)

    affiliate_url = (
        f"https://www.amazon.co.jp/s?k={refined_query}"
        f"&tag={_PARTNER_TAG}"
    )

    try:
        timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # TODO: Creators APIの正式エンドポイント・署名実装をここに追加
            # qualifying sales未達の場合はAPIが403を返す → Noneでフォールバック
            # 現状: 認証情報はあるが実装待ちのため即Noneを返す
            logger.info(
                "[Creators API] 認証情報あり / qualifying sales条件チェック中"
                " → 実装待ち、SearXNGにフォールバック"
            )
            return None

    except aiohttp.ClientResponseError as exc:
        # 403: qualifying sales未達, 401: 認証エラー
        logger.warning("[Creators API] HTTPエラー status=%d → フォールバック", exc.status)
        return None
    except Exception as exc:
        logger.warning("[Creators API] エラー: %s → フォールバック", exc)
        return None
