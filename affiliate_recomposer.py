"""
affiliate_recomposer.py
=======================
【フェーズB - モジュール8 / 最終モジュール】
ソート済みの PricedItem リストを受け取り、各モールの商品URLに
アフィリエイトパラメータを合成した上で rank（最安値順位）を付番し、
最終出力型 AffiliateItem のリストを返す。

【設計方針】
- urllib.parse のみ使用（外部ライブラリ不要、Cloudflare Workers 対応）
- 機密情報（各モールのアフィリエイトID）は引数または環境変数から受け取る
- 既にアフィリエイトURLが設定されている場合は二重変換を防止する
- ID が空文字 / None の場合は元URLをそのまま出力しエラーで止まらない

【各モールのアフィリエイトURL合成仕様】

  ■ Amazon アソシエイト
    方式  : クエリパラメータ tag={amazon_tag} を付与（既存 tag は上書き）
    環境変数: AMAZON_PARTNER_TAG（amazon_api.py と共通）
    例    : https://www.amazon.co.jp/dp/B09XXX?tag=yoursite-22

  ■ 楽天アフィリエイト
    方式  : hb.afl.rakuten.co.jp へのリダイレクトURLにラップ
            rakuten_api.py が既にアフィリエイトURLを取得済みの場合はそのまま使用
    環境変数: RAKUTEN_AFFILIATE_ID（affiliate_id = "xxxxxxxx.yyyyyyyy" 形式）
    例    : https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?pc={encoded_url}&m={encoded_url}

  ■ Yahoo!ショッピング（バリューコマース）
    方式  : ck.jp.ap.valuecommerce.com へのリダイレクトURLにラップ
            yahoo_api.py が既にアフィリエイトURLを取得済みの場合はそのまま使用
    環境変数: YAHOO_AFFILIATE_ID（形式: "{sid}_{pid}" または "{sid}" のみ）
    例    : https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded_url}

【二重変換防止のためのドメイン判定】
  以下のドメインが url に含まれていれば既に変換済みとして合成をスキップ:
    Amazon  : amazon-adsystem.com / amzn.to（短縮URL）
    楽天    : afl.rakuten.co.jp
    Yahoo   : valuecommerce.com

【rank 付番ルール】
  ソート済みリストのインデックス（0始まり）+ 1 を rank とする。
  rank=1 が最安値の商品を指す。
"""

from __future__ import annotations

import logging
import os
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from app.models.schemas import AffiliateItem, MallType, PricedItem

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 既変換ドメイン（二重変換防止）
# ──────────────────────────────────────────────

_AMAZON_AFFILIATE_DOMAINS  = ("amazon-adsystem.com", "amzn.to")
_RAKUTEN_AFFILIATE_DOMAINS = ("afl.rakuten.co.jp",)
_YAHOO_AFFILIATE_DOMAINS   = ("valuecommerce.com",)


# ──────────────────────────────────────────────
# 環境変数ローダー
# ──────────────────────────────────────────────

def _load_affiliate_ids(
    amazon_tag:  str | None,
    rakuten_id:  str | None,
    yahoo_id:    str | None,
) -> tuple[str, str, str]:
    """
    アフィリエイトIDを引数または環境変数から取得する。

    優先順位（各モールとも共通）:
        1. 引数が None でも空文字でもなければそれを使用
        2. 環境変数にフォールバック
        3. 最終的に空文字（= アフィリエイト変換なし）

    環境変数:
        AMAZON_PARTNER_TAG   : Amazon アソシエイト ID
        RAKUTEN_AFFILIATE_ID : 楽天アフィリエイト ID
        YAHOO_AFFILIATE_ID   : Yahoo! バリューコマース ID

    Args:
        amazon_tag  : Amazon アソシエイトタグ（明示的に渡す場合）
        rakuten_id  : 楽天アフィリエイト ID（明示的に渡す場合）
        yahoo_id    : Yahoo! アフィリエイト ID（明示的に渡す場合）

    Returns:
        (amazon_tag, rakuten_id, yahoo_id) のタプル（未設定は空文字列）
    """
    def _resolve(arg: str | None, env_key: str) -> str:
        if arg:
            return arg
        return os.environ.get(env_key, "")

    return (
        _resolve(amazon_tag,  "AMAZON_PARTNER_TAG"),
        _resolve(rakuten_id,  "RAKUTEN_AFFILIATE_ID"),
        _resolve(yahoo_id,    "YAHOO_AFFILIATE_ID"),
    )


# ──────────────────────────────────────────────
# モール別 URL 合成ロジック
# ──────────────────────────────────────────────

def _build_amazon_affiliate_url(url: str, tag: str) -> str:
    """
    Amazon 商品 URL にアソシエイトタグ（tag パラメータ）を付与する。

    処理:
        - urllib.parse でURLを分解し、query パラメータを辞書化
        - "tag" キーを上書き（既存の古いタグを安全に置換）
        - 再組み立てして返す

    二重変換防止:
        url に amazon-adsystem.com / amzn.to が含まれていれば即返却

    Args:
        url : Amazon 商品の生URL
        tag : アソシエイトタグ（例: "yoursite-22"）

    Returns:
        tag パラメータ付きの URL（tag 未設定時は元URLをそのまま返す）
    """
    if not tag:
        return url

    # 既にアフィリエイトドメインが含まれていれば変換不要
    if any(d in url for d in _AMAZON_AFFILIATE_DOMAINS):
        logger.debug("Amazon URL は既にアフィリエイト変換済みのためスキップ: %s", url[:60])
        return url

    try:
        parsed = urlparse(url)
        # parse_qs は値をリスト形式で返すため、フラット辞書に変換する
        params: dict[str, str] = {
            k: v[0] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()
        }
        params["tag"] = tag     # 既存 tag を上書き
        new_query = urlencode(params)
        return urlunparse(parsed._replace(query=new_query))
    except Exception as exc:
        logger.warning("Amazon アフィリエイトURL合成に失敗しました: %s | url=%s", exc, url[:60])
        return url  # フォールバック: 元URLをそのまま返す


def _build_rakuten_affiliate_url(url: str, affiliate_id: str) -> str:
    """
    楽天市場の商品URLをアフィリエイトリダイレクトURLにラップする。

    ラップ形式:
        https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?pc={encoded_url}&m={encoded_url}

    pc  : PC向けの遷移先URL（URLエンコード済み）
    m   : モバイル向けの遷移先URL（同一URLを設定）

    二重変換防止:
        url に afl.rakuten.co.jp が含まれていれば既にアフィリエイトURL
        → そのまま返す（rakuten_api.py が取得済みの affiliateUrl を優先）

    Args:
        url          : 楽天市場商品の生URL
        affiliate_id : 楽天アフィリエイト ID（例: "xxxxxxxx.yyyyyyyy"）

    Returns:
        アフィリエイトリダイレクトURL（ID 未設定時は元URLをそのまま返す）
    """
    if not affiliate_id:
        return url

    # 既にアフィリエイトドメインが含まれていれば変換不要
    if any(d in url for d in _RAKUTEN_AFFILIATE_DOMAINS):
        logger.debug("楽天URL は既にアフィリエイト変換済みのためスキップ: %s", url[:60])
        return url

    try:
        encoded_url = quote(url, safe="")
        return (
            f"https://hb.afl.rakuten.co.jp/hgc/{affiliate_id}/?"
            f"pc={encoded_url}&m={encoded_url}"
        )
    except Exception as exc:
        logger.warning("楽天アフィリエイトURL合成に失敗しました: %s | url=%s", exc, url[:60])
        return url  # フォールバック


def _build_yahoo_affiliate_url(url: str, affiliate_id: str) -> str:
    """
    Yahoo!ショッピングの商品URLをバリューコマース経由のURLにラップする。

    affiliate_id の形式（2パターンに対応）:
        "{sid}_{pid}" 形式: sid と pid を "_" で分割して組み立て
        "{sid}" 形式のみ   : pid パラメータを省略して組み立て

    ラップ形式（sid + pid あり）:
        https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&pid={pid}&vc_url={encoded_url}

    ラップ形式（sid のみ）:
        https://ck.jp.ap.valuecommerce.com/servlet/referral?sid={sid}&vc_url={encoded_url}

    二重変換防止:
        url に valuecommerce.com が含まれていれば既にアフィリエイトURL
        → そのまま返す（yahoo_api.py が取得済みの affiliateUrl を優先）

    Args:
        url          : Yahoo!ショッピング商品の生URL
        affiliate_id : バリューコマース アフィリエイト ID

    Returns:
        バリューコマース経由のアフィリエイトURL（ID 未設定時は元URLをそのまま返す）
    """
    if not affiliate_id:
        return url

    # 既にアフィリエイトドメインが含まれていれば変換不要
    if any(d in url for d in _YAHOO_AFFILIATE_DOMAINS):
        logger.debug("Yahoo URL は既にアフィリエイト変換済みのためスキップ: %s", url[:60])
        return url

    try:
        parts = affiliate_id.split("_", 1)
        sid   = parts[0]
        pid   = parts[1] if len(parts) > 1 else ""
        encoded_url = quote(url, safe="")

        if pid:
            return (
                f"https://ck.jp.ap.valuecommerce.com/servlet/referral"
                f"?sid={sid}&pid={pid}&vc_url={encoded_url}"
            )
        else:
            return (
                f"https://ck.jp.ap.valuecommerce.com/servlet/referral"
                f"?sid={sid}&vc_url={encoded_url}"
            )
    except Exception as exc:
        logger.warning("Yahoo アフィリエイトURL合成に失敗しました: %s | url=%s", exc, url[:60])
        return url  # フォールバック


def build_affiliate_url(url: str, mall: MallType, amazon_tag: str, rakuten_id: str, yahoo_id: str) -> str:
    """
    モールに応じたアフィリエイトURL合成処理を呼び分けるディスパッチャー。

    Args:
        url        : 商品の生URL
        mall       : 対象ECモール識別子
        amazon_tag : Amazon アソシエイトタグ
        rakuten_id : 楽天アフィリエイト ID
        yahoo_id   : Yahoo! バリューコマース ID

    Returns:
        アフィリエイトタグ付きURL（IDが空の場合は元URLをそのまま返す）
    """
    if mall == MallType.AMAZON:
        return _build_amazon_affiliate_url(url, amazon_tag)
    elif mall == MallType.RAKUTEN:
        return _build_rakuten_affiliate_url(url, rakuten_id)
    elif mall == MallType.YAHOO:
        return _build_yahoo_affiliate_url(url, yahoo_id)
    else:
        logger.warning("未知のモール '%s' のためURL変換をスキップ", mall)
        return url


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

def recompose_affiliate_urls(
    sorted_items: list[PricedItem],
    amazon_tag:   str | None = None,
    rakuten_id:   str | None = None,
    yahoo_id:     str | None = None,
) -> list[AffiliateItem]:
    """
    ソート済みの PricedItem リストに対して:
        1. 各アイテムの URL をアフィリエイト URL に変換
        2. ソート順に従った rank（1 始まり）を付番
        3. AffiliateItem として返す

    処理はすべて同期・純粋関数（I/O なし）のため非同期化は不要。

    Args:
        sorted_items : sorter が最安値順にソートした PricedItem リスト
                       （インデックス 0 が最安値 = rank 1）
        amazon_tag   : Amazon アソシエイトタグ（None の場合は環境変数から取得）
        rakuten_id   : 楽天アフィリエイト ID（None の場合は環境変数から取得）
        yahoo_id     : Yahoo! バリューコマース ID（None の場合は環境変数から取得）

    Returns:
        rank 付きの AffiliateItem リスト（そのまま SearchResponse.items に格納できる）
    """
    resolved_amazon, resolved_rakuten, resolved_yahoo = _load_affiliate_ids(
        amazon_tag, rakuten_id, yahoo_id
    )

    results: list[AffiliateItem] = []

    for rank, item in enumerate(sorted_items, start=1):
        affiliate_url = build_affiliate_url(
            url        = item.url,
            mall       = item.mall,
            amazon_tag = resolved_amazon,
            rakuten_id = resolved_rakuten,
            yahoo_id   = resolved_yahoo,
        )

        results.append(AffiliateItem(
            # PricedItem フィールドをそのまま引き継ぎ
            mall            = item.mall,
            item_id         = item.item_id,
            raw_name        = item.raw_name,
            price           = item.price,
            shipping_fee    = item.shipping_fee,
            point           = item.point,
            coupon_discount = item.coupon_discount,
            image_url       = item.image_url,
            seller_name     = item.seller_name,
            review_count    = item.review_count,
            review_score    = item.review_score,
            capacity_ml     = item.capacity_ml,
            quantity        = item.quantity,
            lot             = item.lot,
            parsed_by       = item.parsed_by,
            effective_total = item.effective_total,
            total_units     = item.total_units,
            unit_price      = item.unit_price,
            # affiliate_recomposer が追加するフィールド
            affiliate_url   = affiliate_url,
            rank            = rank,
        ))

    logger.info(
        "recompose_affiliate_urls 完了: %d件 | amazon_tag=%s, rakuten_id=%s, yahoo_id=%s",
        len(results),
        resolved_amazon or "(未設定)",
        resolved_rakuten or "(未設定)",
        resolved_yahoo   or "(未設定)",
    )
    return results
