"""
calculator.py
=============
【フェーズB - モジュール6】
ParsedItem を受け取り、以下の計算式で「1個あたり価格（単価）」を算出して
PricedItem として返す。

【計算ロジック全体】

  ■ 実質総額（effective_total）
      effective_total = price + shipping_fee - coupon_discount - int(point)
      ※ マイナスになった場合は 0 にクランプ（実質無料として扱う）

  ■ 総個数（total_units）
      total_units = quantity × lot
      ※ 0 になった場合（量・ロットが異常値の場合）は 1 にクランプ（ゼロ除算防止）

  ■ 1個あたり単価の算出と整数部・小数部の分離
      unit_cents   = (effective_total × 100) // total_units  ← 整数演算（端数切り捨て）
      integer_part = unit_cents // 100
      decimal_part = unit_cents % 100                        ← 0〜99 のセント単位

      浮動小数点演算を使わず「× 100 してから整数除算」することで、
      Python の float 丸め誤差（例: 3980/24 = 165.83333...）を完全に回避する。

【UnitPrice.decimal_part の仕様】
  schemas.py で ge=0, le=99 と定義されたセント単位の小数部。
  「¥165.83」の場合 → integer_part=165, decimal_part=83
  「¥24.50」の場合  → integer_part=24,  decimal_part=50
  端数は常に切り捨て（購入者側に有利な方向）。
"""

from __future__ import annotations

import logging

from schemas import ParsedItem, PricedItem, UnitPrice

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 実質総額の計算
# ──────────────────────────────────────────────

def _calc_effective_total(item: ParsedItem) -> int:
    """
    実質支払い総額（円・整数）を計算する。

    計算式:
        effective_total = price + shipping_fee - coupon_discount - int(point)

    クランプ:
        ポイント・クーポンが超過してマイナスになる場合は 0 に切り上げる。
        （「実質タダ」として扱い、後段の除算でゼロになることはあっても
           マイナスの単価という矛盾を防ぐ）

    Args:
        item: パーサー処理済みアイテム

    Returns:
        実質総額（0 以上の整数）

    Examples:
        >>> # 通常ケース
        >>> item.price=3980, shipping_fee=0, coupon_discount=200, point=39.0
        >>> _calc_effective_total(item)  # 3980+0-200-39 = 3741
        3741
        >>> # ポイント超過でマイナスになるケース
        >>> item.price=100, coupon_discount=0, point=500.0
        >>> _calc_effective_total(item)  # max(100-500, 0) = 0
        0
    """
    raw = (
        item.price
        + item.shipping_fee
        - item.coupon_discount
        - int(item.point)   # point は float のため int 変換（小数点以下切り捨て）
    )
    return max(raw, 0)


# ──────────────────────────────────────────────
# 総個数の計算
# ──────────────────────────────────────────────

def _calc_total_units(item: ParsedItem) -> int:
    """
    総個数（= 入数 × ロット数）を計算する。

    クランプ:
        結果が 0 以下になった場合（データ異常）は 1 に切り上げる。
        ゼロ除算（ZeroDivisionError）を絶対に発生させない。

    Args:
        item: パーサー処理済みアイテム

    Returns:
        総個数（1 以上の整数）

    Examples:
        >>> item.quantity=24, lot=2  → 48
        >>> item.quantity=1,  lot=1  → 1
        >>> item.quantity=0,  lot=3  → 1（0クランプ）
    """
    total = item.quantity * item.lot
    return max(total, 1)


# ──────────────────────────────────────────────
# 単価の算出と整数部・小数部の分離
# ──────────────────────────────────────────────

def _calc_unit_price(effective_total: int, total_units: int) -> UnitPrice:
    """
    実質総額と総個数から UnitPrice（整数部・小数部分離）を算出する。

    【整数演算による浮動小数点誤差の回避】
        Python の float 除算では端数誤差が生じる場合がある。
        （例: 3980 / 24 = 165.83333...33 → float の精度限界で微妙にずれることがある）
        本関数では「× 100 してから整数除算（//）」を行うことで誤差を完全に排除し、
        UnitPrice.decimal_part の範囲制約（0〜99）を数学的に保証する。

    算出手順:
        unit_cents   = (effective_total × 100) // total_units   ← 整数演算
        integer_part = unit_cents // 100
        decimal_part = unit_cents % 100                         ← 必ず 0〜99

    端数処理: 切り捨て（購入者に有利な方向）

    Args:
        effective_total: 実質総額（0 以上の整数）
        total_units:     総個数（1 以上の整数）

    Returns:
        UnitPrice（integer_part と decimal_part に分離済み）

    Examples:
        >>> _calc_unit_price(3980, 24)
        UnitPrice(integer_part=165, decimal_part=83)   # 165.83円/個

        >>> _calc_unit_price(3980, 1)
        UnitPrice(integer_part=3980, decimal_part=0)   # 3980.00円/個

        >>> _calc_unit_price(100, 3)
        UnitPrice(integer_part=33, decimal_part=33)    # 33.33円/個

        >>> _calc_unit_price(0, 10)
        UnitPrice(integer_part=0, decimal_part=0)      # 0.00円/個（実質無料）
    """
    # セント単位に拡大してから整数除算することで float を使わない
    unit_cents   = (effective_total * 100) // total_units
    integer_part = unit_cents // 100
    decimal_part = unit_cents % 100     # % 100 の結果は数学的に必ず 0〜99

    return UnitPrice(integer_part=integer_part, decimal_part=decimal_part)


# ──────────────────────────────────────────────
# ParsedItem → PricedItem 変換
# ──────────────────────────────────────────────

def _calculate_single(item: ParsedItem) -> PricedItem:
    """
    ParsedItem 1件から実質総額・総個数・単価を計算して PricedItem を返す。

    Args:
        item: パーサー処理済みの商品データ

    Returns:
        unit_price（整数部・小数部）・effective_total・total_units を含む PricedItem
    """
    effective_total = _calc_effective_total(item)
    total_units     = _calc_total_units(item)
    unit_price      = _calc_unit_price(effective_total, total_units)

    logger.debug(
        "calculate: '%s' → effective_total=%d, total_units=%d, unit_price=%d.%02d",
        item.raw_name[:40],
        effective_total,
        total_units,
        unit_price.integer_part,
        unit_price.decimal_part,
    )

    return PricedItem(
        # ParsedItem フィールドをそのまま引き継ぎ
        mall            = item.mall,
        item_id         = item.item_id,
        url             = item.url,
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
        # calculator が追加するフィールド
        effective_total = effective_total,
        total_units     = total_units,
        unit_price      = unit_price,
    )


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

def calculate_unit_price(parsed_item: ParsedItem) -> PricedItem:
    """
    ParsedItem 1件の単価を計算して PricedItem を返す。

    Args:
        parsed_item: パーサー処理済みの商品データ

    Returns:
        unit_price を含む PricedItem
    """
    return _calculate_single(parsed_item)


def calculate_all(parsed_items: list[ParsedItem]) -> list[PricedItem]:
    """
    ParsedItem のリスト全件に単価計算を適用するバッチ処理。

    同期処理（純粋な算術演算）のため非同期処理は不要。
    Cloudflare Workers の軽量環境でも高速に動作する。

    Args:
        parsed_items: 全アイテムのパーサー済みリスト

    Returns:
        PricedItem のリスト（入力と同じ順序）
    """
    results = [_calculate_single(item) for item in parsed_items]

    logger.info(
        "calculate_all 完了: %d件処理",
        len(results),
    )
    return results
