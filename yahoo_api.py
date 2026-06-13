"""
yahoo_api.py
============
【フェーズA - モジュール3】
Yahoo!ショッピング商品検索API（Shopping Web Service V3）から商品情報を取得し、
RawItem のリストとして返す。

【設計方針】
- 認証は applicationId（client_id）をクエリパラメータに付与するだけ。
  OAuth や署名生成は不要で3モールの中で最もシンプルな認証方式。
- 非同期 HTTP クライアントに pyodide.http.pyfetch を使用（Cloudflare Workers ネイティブ対応）
- 機密情報（client_id / affiliate_id）はすべて環境変数から受け取る

【Yahoo!ショッピングのポイント自動計算の仕様】
  API レスポンスの point オブジェクト:
      point.amount : 商品ページに表示されているポイント付与数（円換算済み整数）
      point.times  : ポイント倍率（整数）

  優先順位:
      1. point.amount が 1 以上の場合 → そのまま float に変換して使用
      2. point.amount が 0 または未取得の場合 → 基本1%をフォールバック計算
             ポイント額 = math.floor(price × 1 / 100)

  ※ Yahoo!ショッピングの基本付与レートは「1% = 1円相当/100円」。
    SPU（スーパーポイントアッププログラム）等による上乗せは
    会員ステータスに依存するため、全員確実に受け取れる基本分のみを対象とする。

【Yahoo!ショッピング商品検索API v3 基本仕様】
  エンドポイント : https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch
  HTTPメソッド   : GET
  hits 上限      : 50件/リクエスト
  start 上限     : 2001（start + hits <= 2001 の範囲内）

【送料フラグ（shipping.code）の扱い】
  1: 送料無料     → shipping_fee = 0
  2: 条件付送料無料 → 0 として扱う
  3: 送料別       → 金額は API から取得不可のため 0（後続モジュールで補完）
  その他          → 0 として扱う
"""

from __future__ import annotations

import logging
import math
import os
from typing import Any
from urllib.parse import urlencode

from pyodide.http import pyfetch

from app.models.schemas import MallType, RawItem

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Yahoo! API 定数
# ──────────────────────────────────────────────

_YAHOO_API_BASE   = (
    "https://shopping.yahooapis.jp/ShoppingWebService/V3/itemSearch"
)
_MAX_HITS_PER_REQ = 50      # API の 1リクエストあたり最大件数
_START_MAX        = 2001    # start パラメータの上限（start + hits <= 2001）

# 関連度順で取得（最安値順にはせず、後段の sorter に委ねる）
_DEFAULT_SORT = "-score"


# ──────────────────────────────────────────────
# 環境変数ローダー
# ──────────────────────────────────────────────

def _load_credentials() -> tuple[str, str, str]:
    """
    環境変数から Yahoo! ショッピング API の認証・アフィリエイト情報を取得する。

    環境変数:
        YAHOO_CLIENT_ID      : Yahoo!デベロッパーネットワーク アプリケーションID（必須）
        YAHOO_AFFILIATE_TYPE : アフィリエイトタイプ（任意。"vc" / "none"）
        YAHOO_AFFILIATE_ID   : バリューコマース アフィリエイトID（任意）

    Returns:
        (client_id, affiliate_type, affiliate_id) のタプル。
        affiliate_type / affiliate_id は未設定なら空文字列。

    Raises:
        EnvironmentError: client_id が未設定の場合
    """
    client_id      = os.environ.get("YAHOO_CLIENT_ID", "")
    affiliate_type = os.environ.get("YAHOO_AFFILIATE_TYPE", "")
    affiliate_id   = os.environ.get("YAHOO_AFFILIATE_ID", "")

    if not client_id:
        raise EnvironmentError(
            "Yahoo! ショッピング API の認証情報が未設定です: YAHOO_CLIENT_ID"
        )

    return client_id, affiliate_type, affiliate_id


# ──────────────────────────────────────────────
# ポイント計算
# ──────────────────────────────────────────────

def _calculate_point(price: int, point_amount: int | None) -> float:
    """
    Yahoo!ショッピングのポイント付与額（円換算）を決定する。

    優先順位:
        1. API の point.amount が 1 以上 → そのまま採用
        2. 上記以外（0 / None / 取得不可）→ 基本1%でフォールバック計算

    Args:
        price        : 商品価格（円・税込）
        point_amount : API レスポンスの point.amount（整数 or None）

    Returns:
        ポイント付与額（float。RawItem.point の型に合わせる）

    Examples:
        >>> _calculate_point(3980, 80)    # API値を優先
        80.0
        >>> _calculate_point(3980, 0)     # フォールバック: floor(3980 * 1 / 100)
        39.0
        >>> _calculate_point(3980, None)  # フォールバック
        39.0
        >>> _calculate_point(99, None)    # 端数切り捨て
        0.0
    """
    # API 値を優先
    if point_amount is not None and point_amount > 0:
        return float(point_amount)

    # フォールバック: 基本1%
    if price <= 0:
        return 0.0
    return float(math.floor(price * 1 / 100))


# ──────────────────────────────────────────────
# URLビルダー
# ──────────────────────────────────────────────

def _build_search_url(
    keyword:        str,
    client_id:      str,
    affiliate_type: str,
    affiliate_id:   str,
    hits:           int,
    start:          int,
) -> str:
    """
    Yahoo!ショッピング商品検索 API の GET リクエスト URL を組み立てる。

    ページネーションは hits / page ではなく start（開始インデックス）方式。

    Args:
        keyword        : 検索キーワード
        client_id      : Yahoo! API アプリケーションID
        affiliate_type : アフィリエイトタイプ（"vc" / 空文字列）
        affiliate_id   : バリューコマース アフィリエイトID（空文字列の場合は省略）
        hits           : 1リクエストあたりの取得件数（1〜50）
        start          : 取得開始インデックス（1始まり）

    Returns:
        クエリパラメータ付きの完全な URL 文字列
    """
    params: dict[str, Any] = {
        "appid":     client_id,
        "query":     keyword,
        "hits":      hits,
        "start":     start,
        "sort":      _DEFAULT_SORT,
        "condition": "new",     # 新品のみ
    }
    if affiliate_type:
        params["affiliate_type"] = affiliate_type
    if affiliate_id:
        params["affiliate_id"] = affiliate_id

    return f"{_YAHOO_API_BASE}?{urlencode(params)}"


# ──────────────────────────────────────────────
# レスポンス → RawItem 変換
# ──────────────────────────────────────────────

def _parse_item(item: dict[str, Any]) -> RawItem | None:
    """
    Yahoo!ショッピング商品検索 API の Result[] 要素 1件を RawItem に変換する。

    変換できない場合（価格なし・商品名なし等）は None を返し、
    呼び出し元でスキップさせる。

    Args:
        item : API レスポンスの ResultSet.Result 配列の1要素

    Returns:
        RawItem または None

    【フィールドマッピング詳細】
        code（itemCode相当）→ item_id  ※ないため url のパスから生成
        name               → raw_name
        price              → price    （税込整数）
        url                → url      （生URL。affiliate_recomposer で変換）
        affiliateUrl       → url      （存在する場合は優先）
        0 固定             → shipping_fee（API から金額取得不可）
        point.amount or 1% → point    （_calculate_point で決定）
        0 固定             → coupon_discount（商品検索APIでは取得不可）
        image.medium       → image_url
        seller.name        → seller_name
        review.count       → review_count
        review.rate        → review_score
    """
    # ── 必須フィールド: 商品名 ──
    raw_name: str = item.get("name", "")
    if not raw_name:
        logger.debug("name が取得できなかったアイテムをスキップします")
        return None

    # ── 必須フィールド: 価格 ──
    try:
        price = int(item["price"])
    except (KeyError, TypeError, ValueError):
        logger.debug("price が取得できなかったアイテムをスキップします: name=%s", raw_name[:30])
        return None
    if price <= 0:
        logger.debug("有効な価格がないアイテムをスキップします: name=%s", raw_name[:30])
        return None

    # ── 商品ID（Yahoo!はアイテム固有IDがフラットに存在しないため code を使用）──
    # code フィールドは "shopId:itemCode" 形式で返ることがある
    item_id: str = item.get("code", "") or item.get("itemCode", "")
    if not item_id:
        # URL の末尾パスをフォールバック IDとして使用
        raw_url_tmp: str = item.get("url", "")
        item_id = raw_url_tmp.rstrip("/").split("/")[-1] or raw_name[:50]

    # ── 商品URL ──
    # affiliateUrl が存在する場合はそちらを優先して格納する。
    # affiliate_recomposer での再合成が前提だが、
    # すでにアフィリエイトタグ付きの URL が返ってくれる場合は活用する。
    url: str = (
        item.get("affiliateUrl")
        or item.get("url")
        or ""
    )
    if not url:
        logger.debug("url が取得できなかったアイテムをスキップします: name=%s", raw_name[:30])
        return None

    # ── ポイント計算 ──
    # point.amount が API から提供される場合はそれを優先し、
    # ない場合は基本 1% フォールバック。
    point_amount: int | None = None
    try:
        point_obj = item.get("point")
        if point_obj and isinstance(point_obj, dict):
            pa = point_obj.get("amount")
            if pa is not None:
                point_amount = int(pa)
    except (TypeError, ValueError):
        pass
    point = _calculate_point(price, point_amount)

    # ── 送料（一律 0）──
    # shipping.code で送料無料フラグは判別できるが金額は不明のため 0 固定。
    # postageFlag 同様、後続モジュールでの補完を前提とする。
    shipping_fee = 0

    # ── 商品画像URL ──
    image_url: str | None = None
    try:
        image_obj = item.get("image")
        if image_obj and isinstance(image_obj, dict):
            image_url = image_obj.get("medium") or image_obj.get("small")
    except TypeError:
        pass

    # ── 出品者（ショップ）名 ──
    seller_name: str | None = None
    try:
        seller_obj = item.get("seller")
        if seller_obj and isinstance(seller_obj, dict):
            seller_name = seller_obj.get("name") or None
    except TypeError:
        pass

    # ── レビュー情報 ──
    # Yahoo! API はアイテムレベルと seller レベルの両方にレビューが存在する。
    # アイテムレベル（review）を優先し、存在しなければ seller.review を使用。
    review_count: int | None = None
    review_score: float | None = None
    try:
        review_obj = (
            item.get("review")
            or (item.get("seller") or {}).get("review")
        )
        if review_obj and isinstance(review_obj, dict):
            rc = review_obj.get("count")
            rr = review_obj.get("rate")
            if rc is not None:
                review_count = int(rc)
            if rr is not None:
                score = float(rr)
                review_score = score if score > 0.0 else None
    except (TypeError, ValueError):
        pass

    return RawItem(
        mall            = MallType.YAHOO,
        item_id         = item_id,
        url             = url,
        raw_name        = raw_name,
        price           = price,
        shipping_fee    = shipping_fee,
        point           = point,
        coupon_discount = 0,        # 商品検索 API では個別クーポン取得不可のため 0 固定
        image_url       = image_url,
        seller_name     = seller_name,
        review_count    = review_count,
        review_score    = review_score,
    )


# ──────────────────────────────────────────────
# Yahoo! API 1ページ分リクエスト実行
# ──────────────────────────────────────────────

async def _request_search_items(
    keyword:        str,
    client_id:      str,
    affiliate_type: str,
    affiliate_id:   str,
    hits:           int,
    start:          int,
) -> dict[str, Any]:
    """
    Yahoo!ショッピング商品検索 API を1回呼び出してレスポンス JSON を返す。
    Cloudflare Workers ネイティブの pyfetch を使用。

    Args:
        keyword        : 検索キーワード
        client_id      : Yahoo! API アプリケーションID
        affiliate_type : アフィリエイトタイプ
        affiliate_id   : バリューコマース アフィリエイトID
        hits           : 1リクエストあたりの取得件数
        start          : 取得開始インデックス（1始まり）

    Returns:
        API レスポンスの辞書

    Raises:
        Exception : 4xx / 5xx またはネットワークエラーの場合
    """
    url = _build_search_url(keyword, client_id, affiliate_type, affiliate_id, hits, start)
    response = await pyfetch(url, method="GET")

    if not response.ok:
        body_text = await response.string()
        raise Exception(
            f"Yahoo! API エラー: status={response.status} body={body_text[:500]}"
        )

    return await response.json()


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

async def fetch_yahoo_items(
    keyword: str,
    limit:   int = 50,
) -> list[RawItem]:
    """
    Yahoo!ショッピング商品検索 API でキーワード検索を行い、RawItem のリストを返す。

    Yahoo! API のページネーションは page 番号ではなく start（開始インデックス）方式。
    limit が 50 を超える場合は start をずらしながら複数リクエストを行い結合して返す。

    Args:
        keyword : 検索キーワード（例: "スーパードライ"）
        limit   : 取得したい最大件数（API 上限: start + hits <= 2001）

    Returns:
        RawItem のリスト。価格情報がない商品は除外済み。

    Raises:
        EnvironmentError     : 認証情報の環境変数が未設定の場合
        Exception: APIからエラーレスポンスが返った場合またはネットワークエラーの場合
    """
    client_id, affiliate_type, affiliate_id = _load_credentials()

    results:   list[RawItem] = []
    remaining: int           = limit
    start:     int           = 1

    while remaining > 0:
            # start の上限チェック（start + hits - 1 <= 2000）
            if start >= _START_MAX:
                logger.debug("start=%d が上限 %d に達しました。検索終了。", start, _START_MAX)
                break

            hits = min(remaining, _MAX_HITS_PER_REQ, _START_MAX - start)

            try:
                response_json = await _request_search_items(
                    keyword        = keyword,
                    client_id      = client_id,
                    affiliate_type = affiliate_type,
                    affiliate_id   = affiliate_id,
                    hits           = hits,
                    start          = start,
                )
            except Exception as exc:
                logger.error("Yahoo! API リクエストエラー: %s", exc)
                break

            # Result 配列の取り出し
            items_raw: list[dict] = (
                response_json
                .get("hits", [])          # V3 フラット形式
            )
            # V3 は hits キーに直接配列が入る場合と ResultSet 配下の場合がある
            if not items_raw:
                items_raw = (
                    response_json
                    .get("ResultSet", {})
                    .get("Result", [])
                )

            if not items_raw:
                logger.debug("start=%d: 取得アイテム数 0。検索終了。", start)
                break

            for item_raw in items_raw:
                raw_item = _parse_item(item_raw)
                if raw_item is not None:
                    results.append(raw_item)

            returned: int = len(items_raw)
            logger.debug(
                "start=%d: %d件取得（有効: %d件）",
                start, returned, len(results),
            )

            # 次ページの start を更新
            start     += returned
            remaining -= returned

            # 実際に返った件数が要求より少ない場合は最終ページ
            if returned < hits:
                logger.debug("返却件数 %d < 要求件数 %d。検索終了。", returned, hits)
                break

            # 総件数チェックによる早期終了
            total_available: int = (
                response_json.get("totalResultsAvailable")
                or response_json.get("ResultSet", {}).get("totalResultsAvailable")
                or 0
            )
            if total_available and start > total_available:
                logger.debug("総件数 %d を超えました。検索終了。", total_available)
                break

    logger.info(
        "fetch_yahoo_items 完了: keyword='%s' 取得件数=%d", keyword, len(results)
    )
    return results