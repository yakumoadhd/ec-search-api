"""
rakuten_api.py
==============
【フェーズA - モジュール2】
楽天市場商品検索API（Rakuten Ichiba Item Search API）から商品情報を取得し、
RawItem のリストとして返す。

【設計方針】
- 認証方式が Amazon SigV4 と異なり、クエリパラメータに applicationId と
  accessKey を付与するだけのシンプルな GET リクエストで完結する
- 非同期 HTTP クライアントに aiohttp を使用
- 機密情報（applicationId / accessKey / affiliateId）はすべて環境変数から受け取る

【楽天ポイント自動計算の仕様】
  楽天の基本ポイントは「1倍 = 購入金額の 1%」という仕組み。
  API レスポンスの pointRate フィールドがそのまま「倍率」を示す整数。

  計算式:
      ポイント額（円） = math.floor(itemPrice × pointRate / 100)

  例:
      itemPrice=3980, pointRate=1  → floor(3980×1/100)  = 39 ポイント
      itemPrice=3980, pointRate=5  → floor(3980×5/100)  = 199 ポイント
      itemPrice=3980, pointRate=10 → floor(3980×10/100) = 398 ポイント

【楽天市場商品検索API 2026年新基盤仕様】
  エンドポイント : https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701
  HTTPメソッド   : GET
  認証           : applicationId + accessKey（両方必須）
  formatVersion  : 2（Items が配列で返る形式）
  hits 上限      : 30件/リクエスト
  page 上限      : 100ページ

【postageFlag（送料フラグ）の扱い】
  0: 送料込 → shipping_fee = 0
  1: 送料別 → 金額は API から取得不可のため 0 として扱う（後続モジュールで補完）
  2: 条件付送料無料 → 0 として扱う
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any
from urllib.parse import urlencode

import aiohttp

from schemas import MallType, RawItem

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 楽天 API 定数
# ──────────────────────────────────────────────

_RAKUTEN_API_BASE = (
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"
)
_MAX_HITS_PER_REQ = 30
_MAX_PAGE         = 100
_FORMAT_VERSION   = 2
_DEFAULT_SORT     = "standard"


# ──────────────────────────────────────────────
# 環境変数ローダー
# ──────────────────────────────────────────────

def _load_credentials() -> tuple[str, str, str]:
    """
    環境変数から楽天 API の認証情報を取得する。

    環境変数:
        RAKUTEN_APPLICATION_ID : 楽天 API アプリ ID（必須）
        RAKUTEN_ACCESS_KEY     : 楽天 API アクセスキー（必須・2026年新基盤から必須）
        RAKUTEN_AFFILIATE_ID   : 楽天アフィリエイト ID（任意）

    Returns:
        (application_id, access_key, affiliate_id) のタプル。
        affiliate_id は未設定なら空文字列。

    Raises:
        EnvironmentError: application_id または access_key が未設定の場合
    """
    application_id = os.environ.get("RAKUTEN_APPLICATION_ID", "")
    access_key     = os.environ.get("RAKUTEN_ACCESS_KEY", "")
    affiliate_id   = os.environ.get("RAKUTEN_AFFILIATE_ID", "")

    if not application_id:
        raise EnvironmentError(
            "楽天 API の認証情報が未設定です: RAKUTEN_APPLICATION_ID"
        )
    if not access_key:
        raise EnvironmentError(
            "楽天 API の認証情報が未設定です: RAKUTEN_ACCESS_KEY"
        )

    return application_id, access_key, affiliate_id


# ──────────────────────────────────────────────
# ポイント計算
# ──────────────────────────────────────────────

def _calculate_point(price: int, point_rate: int) -> float:
    """
    楽天ポイント付与額（円換算）を計算する。

    楽天の基本ルール:
        「1倍 = 購入金額の 1%」
        端数は切り捨て（楽天の実際の付与ルールに準拠）

    Args:
        price      : 商品価格（円・税込）
        point_rate : API レスポンスの pointRate（整数倍率）

    Returns:
        ポイント付与額（float。RawItem.point の型に合わせる）

    Examples:
        >>> _calculate_point(3980, 1)
        39.0
        >>> _calculate_point(3980, 5)
        199.0
        >>> _calculate_point(3980, 10)
        398.0
    """
    if price <= 0 or point_rate <= 0:
        return 0.0
    return float(math.floor(price * point_rate / 100))


# ──────────────────────────────────────────────
# URLビルダー
# ──────────────────────────────────────────────

def _build_search_url(
    keyword:        str,
    application_id: str,
    access_key:     str,
    affiliate_id:   str,
    hits:           int,
    page:           int,
) -> str:
    """
    楽天市場商品検索 API の GET リクエスト URL を組み立てる。

    Args:
        keyword        : 検索キーワード
        application_id : 楽天 API アプリ ID
        access_key     : 楽天 API アクセスキー（2026年新基盤から必須）
        affiliate_id   : 楽天アフィリエイト ID（空文字列の場合はパラメータ省略）
        hits           : 1ページあたりの取得件数（1〜30）
        page           : ページ番号（1〜100）

    Returns:
        クエリパラメータ付きの完全な URL 文字列
    """
    params: dict[str, Any] = {
        "applicationId": application_id,
        "accessKey":     access_key,
        "keyword":       keyword,
        "hits":          hits,
        "page":          page,
        "sort":          _DEFAULT_SORT,
        "format":        "json",
        "formatVersion": _FORMAT_VERSION,
    }
    if affiliate_id:
        params["affiliateId"] = affiliate_id

    return f"{_RAKUTEN_API_BASE}?{urlencode(params)}"


# ──────────────────────────────────────────────
# レスポンス → RawItem 変換
# ──────────────────────────────────────────────

def _parse_item(item: dict[str, Any]) -> RawItem | None:
    """
    楽天市場商品検索 API の Items[] 要素 1件を RawItem に変換する。

    変換できない場合（価格なし・商品名なし等）は None を返し、
    呼び出し元でスキップさせる。

    Args:
        item : API レスポンスの Items 配列の1要素（formatVersion=2）

    Returns:
        RawItem または None
    """
    item_code: str = item.get("itemCode", "")
    if not item_code:
        logger.debug("itemCode が取得できなかったアイテムをスキップします")
        return None

    raw_name: str = item.get("itemName", "")
    if not raw_name:
        logger.debug("itemName が取得できなかったアイテムをスキップします: code=%s", item_code)
        return None

    try:
        price = int(item["itemPrice"])
    except (KeyError, TypeError, ValueError):
        logger.debug("itemPrice が取得できなかったアイテムをスキップします: code=%s", item_code)
        return None
    if price <= 0:
        logger.debug("有効な価格がないアイテムをスキップします: code=%s", item_code)
        return None

    try:
        point_rate = int(item.get("pointRate", 1) or 1)
    except (TypeError, ValueError):
        point_rate = 1
    point = _calculate_point(price, point_rate)

    url: str = (
        item.get("affiliateUrl")
        or item.get("itemUrl")
        or f"https://item.rakuten.co.jp/{item_code}/"
    )

    shipping_fee = 0

    image_url: str | None = None
    try:
        medium_images = item.get("mediumImageUrls", [])
        if medium_images:
            first = medium_images[0]
            image_url = first.get("imageUrl") if isinstance(first, dict) else str(first)
    except (IndexError, TypeError):
        pass

    seller_name: str | None = item.get("shopName") or None

    review_count: int | None = None
    review_score: float | None = None
    try:
        rc = item.get("reviewCount")
        if rc is not None:
            review_count = int(rc)
    except (TypeError, ValueError):
        pass
    try:
        ra = item.get("reviewAverage")
        if ra is not None:
            score = float(ra)
            review_score = score if score > 0.0 else None
    except (TypeError, ValueError):
        pass

    return RawItem(
        mall            = MallType.RAKUTEN,
        item_id         = item_code,
        url             = url,
        raw_name        = raw_name,
        price           = price,
        shipping_fee    = shipping_fee,
        point           = point,
        coupon_discount = 0,
        image_url       = image_url,
        seller_name     = seller_name,
        review_count    = review_count,
        review_score    = review_score,
    )


# ──────────────────────────────────────────────
# 楽天 API 1ページ分リクエスト実行
# ──────────────────────────────────────────────

async def _request_search_items(
    keyword:        str,
    application_id: str,
    access_key:     str,
    affiliate_id:   str,
    hits:           int,
    page:           int,
) -> dict[str, Any]:
    url = _build_search_url(keyword, application_id, access_key, affiliate_id, hits, page)
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                body_text = await response.text()
                raise Exception(
                    f"楽天 API エラー: status={response.status} body={body_text[:500]}"
                )
            return await response.json()


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

async def fetch_rakuten_items(
    keyword: str,
    limit:   int = 30,
) -> list[RawItem]:
    """
    楽天市場商品検索 API でキーワード検索を行い、RawItem のリストを返す。

    Args:
        keyword : 検索キーワード（例: "スーパードライ"）
        limit   : 取得したい最大件数

    Returns:
        RawItem のリスト。価格情報がない商品は除外済み。

    Raises:
        EnvironmentError : 認証情報の環境変数が未設定の場合
        Exception        : APIからエラーレスポンスが返った場合またはネットワークエラーの場合
    """
    application_id, access_key, affiliate_id = _load_credentials()

    results:   list[RawItem] = []
    remaining: int           = limit
    page:      int           = 1

    while remaining > 0 and page <= _MAX_PAGE:
        hits = min(remaining, _MAX_HITS_PER_REQ)

        try:
            response_json = await _request_search_items(
                keyword        = keyword,
                application_id = application_id,
                access_key     = access_key,
                affiliate_id   = affiliate_id,
                hits           = hits,
                page           = page,
            )
        except Exception as exc:
            logger.error("楽天 API リクエストエラー: %s", exc)
            break

        items_raw: list[dict] = response_json.get("items", [])

        if not items_raw:
            logger.debug("page=%d: 取得アイテム数 0。検索終了。", page)
            break

        for item_raw in items_raw:
            raw_item = _parse_item(item_raw)
            if raw_item is not None:
                results.append(raw_item)

        logger.debug(
            "page=%d: %d件取得（有効: %d件）",
            page, len(items_raw), len(results),
        )

        remaining -= hits
        page      += 1

        total_page_count: int = response_json.get("pageCount", 1)
        if page > total_page_count:
            logger.debug("総ページ数 %d に到達。検索終了。", total_page_count)
            break

    logger.info(
        "fetch_rakuten_items 完了: keyword='%s' 取得件数=%d", keyword, len(results)
    )
    return results
