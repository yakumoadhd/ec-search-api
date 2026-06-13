"""
sorter.py
=========
【フェーズB - モジュール7】
PricedItem のリストをモール横断で「1個あたり単価（安い順）」にソートして返す。

【設計方針】
- Python 標準の sorted() 関数と tuple ソートキーのみ使用（外部ライブラリ不要）
- 浮動小数点比較を一切使わず整数演算のみでキーを構成し、
  calculator.py の誤差ゼロメリットをソートフェーズまで継承する
- 安定ソート（Timsort）の性質を活かし、同一キーのアイテムの入力順を保持

【ソートキーの設計】

  プライマリキー（1個あたり単価）:
      unit_cents = integer_part × 100 + decimal_part
      ← calculator で採用したセント単位の整数をそのまま復元
         float への変換なし・丸め誤差なし

  セカンダリキー（タイブレーク: 実質総額）:
      effective_total
      ← 単価が同じなら「総支払い額が少ない方（少量まとめ買い）」を上位に
         同じ単価なら消費者の初期出費が少ない方を優先するという設計判断

  ソート方向: 両キーとも昇順（小さいほど上位）
"""

from __future__ import annotations

import logging

from app.models.schemas import PricedItem

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# ソートキー関数
# ──────────────────────────────────────────────

def _sort_key(item: PricedItem) -> tuple[int, int]:
    """
    PricedItem のソートキーを (unit_cents, effective_total) のタプルで返す。

    プライマリキー: unit_cents = integer_part × 100 + decimal_part
        ・calculator が採用したセント単位を復元した整数値
        ・float を使わないため比較時の丸め誤差ゼロを保証
        ・例: ¥165.83/個 → 16583, ¥148.33/個 → 14833

    セカンダリキー: effective_total（実質支払い総額）
        ・単価が完全一致したときに「初期出費が少ない商品」を上位へ
        ・例: 同じ¥165.00/個でも 330円(2個) < 1650円(10個) は前者が上位

    Args:
        item: ソート対象の PricedItem

    Returns:
        (unit_cents, effective_total) のタプル（昇順比較用）
    """
    unit_cents = (
        item.unit_price.integer_part * 100
        + item.unit_price.decimal_part
    )
    return (unit_cents, item.effective_total)


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

def sort_by_unit_price(priced_items: list[PricedItem]) -> list[PricedItem]:
    """
    PricedItem のリストを1個あたり単価の安い順（昇順）にソートして返す。

    ソートキー:
        プライマリ: integer_part × 100 + decimal_part（セント単位整数、昇順）
        タイブレーク: effective_total（実質総額、昇順）

    実装ノート:
        - Python の sorted() は Timsort（安定ソート）を採用しており、
          O(n log n) の時間計算量で動作する
        - key 関数を1回ずつ呼び出す方式（DSU / Schwartzian transform）のため
          アイテム数が多くても比較ごとに key が再計算されず高速
        - 元のリストは変更しない（sorted() は新しいリストを返す）
        - モール（Amazon / 楽天 / Yahoo）の区別なくフラットに比較する

    Args:
        priced_items: calculator が単価を算出した全アイテムのリスト

    Returns:
        最安値順にソートされた新しい PricedItem リスト
    """
    sorted_items = sorted(priced_items, key=_sort_key)

    if sorted_items:
        best  = sorted_items[0]
        worst = sorted_items[-1]
        logger.info(
            "sort_by_unit_price 完了: %d件 | 最安 %d.%02d円/個 (%s) → 最高 %d.%02d円/個 (%s)",
            len(sorted_items),
            best.unit_price.integer_part,  best.unit_price.decimal_part,  best.mall,
            worst.unit_price.integer_part, worst.unit_price.decimal_part, worst.mall,
        )
    else:
        logger.info("sort_by_unit_price: 入力リストが空のため何もしない")

    return sorted_items
