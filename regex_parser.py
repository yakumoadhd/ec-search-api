"""
regex_parser.py
===============
商品名（raw_name）から Python 標準の re モジュールのみを使い、
「容量（capacity_ml）・入数（quantity）・ロット（lot）」を高速抽出する。

【v8.01 変更点】
- Cloudflare Workers(pyodide)依存を完全排除
- FastAPI + aiohttp 環境で動作
- app.models.schemas の参照パスをフラット構成に修正
- 容量・入数パターンを強化（500ml×24本、350ml✕48本 等に確実対応）

【設計方針】
- 外部ライブラリ不使用（Python 標準 re / unicodedata のみ）
- 抽出できなかった項目は None / デフォルト値のまま
  → 後続の ai_parser（Ollama）が補完対象と認識できるようにする
- parsed_by = "regex"

【正規化処理の流れ】
  商品名原文
    ↓ unicodedata.normalize("NFKC")  全角数字・全角英字を半角に一括変換
    ↓ .lower()                       大文字英字を小文字に統一
    ↓ 日本語単位の置換               ミリリットル→ml, リットル→l, etc.
    ↓ 掛け算記号の統一               × ✕ ＊ → x
  正規化済みテキスト → 各パターンマッチへ

【抽出ルールと優先順位】

  ■ 容量（capacity_ml）
    対象単位 : ml / cc / l / g / kg / mg
    抽出後変換:
      ml / cc → そのまま（float, mL 換算）
      l       → × 1000（mL 換算）
      g / kg / mg は重量系として capacity_ml には格納しない
    先頭優先 : 複数マッチ時は最初にヒットしたもの

  ■ 入数（quantity）
    優先度 1: 「容量表記+x数字」形式（例: 350mlx24、500mlx48本）← 最重要
    優先度 2: 「x数字」単独形式（例: x24）
    優先度 3: 「数字+個数単位」形式（例: 24缶、60粒入）
    優先度 4: 「数字+入(り)」の単独表記

  ■ ロット（lot）
    ロット専用キーワード: ケース / 箱（セット）/ 回分
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from schemas import MallType, ParsedItem, RawItem

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 正規化
# ══════════════════════════════════════════════

_JP_UNIT_REPLACEMENTS: list[tuple[str, str]] = [
    ("ミリリットル", "ml"),
    ("ミリリッター", "ml"),
    ("リットル",     "l"),
    ("リッター",     "l"),
    ("キログラム",   "kg"),
    ("ミリグラム",   "mg"),
    ("グラム",       "g"),
    ("×",           "x"),   # 全角掛け算記号
    ("✕",           "x"),   # 特殊掛け算記号
    ("＊",           "x"),   # 全角アスタリスク
    ("*",            "x"),   # 半角アスタリスク
]


def _normalize(text: str) -> str:
    """
    商品名を正規表現マッチ用に正規化する。

    Examples:
        >>> _normalize("アサヒ スーパードライ ３５０ｍｌ×２４缶")
        'アサヒ スーパードライ 350mlx24缶'
        >>> _normalize("コカ・コーラ 500ml✕24本")
        'コカ・コーラ 500mlx24本'
        >>> _normalize("１．５Ｌペットボトル×6本 2ケース")
        '1.5lペットボトルx6本 2ケース'
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.lower()
    for jp, ascii_ in _JP_UNIT_REPLACEMENTS:
        text = text.replace(jp, ascii_)
    return text


# ══════════════════════════════════════════════
# コンパイル済み正規表現パターン
# ══════════════════════════════════════════════

# ── 容量パターン ──────────────────────────────
# 対応例: 350ml / 500ml / 1.5l / 2l / 330cc
_VOLUME_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(ml|cc|l(?![a-wyz])|g(?![a-z])|kg|mg)",
)

# ── 入数パターン群 ─────────────────────────────

# 個数単位リスト
_PACK_UNIT = (
    r"(?:本|缶|袋|個|枚|包|粒|錠|食|杯|組|日|ポーチ|ピース|パック|セット|台|箱|瓶|びん|ボトル)"
    r"(?:分|入り?|組)?"
)

# 優先度 1: 「容量+x数字+任意の単位」形式
# 例: 350mlx24 / 500mlx24本 / 350ml x 48缶
_VOLUME_PACK_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:ml|cc|l|g|kg)\s*x\s*(\d+)\s*" + _PACK_UNIT + r"?",
)

# 優先度 2: 「x数字」単独形式
# 例: x24 / x 6
_PACK_X_RE = re.compile(r"x\s*(\d+)")

# 優先度 3: 「数字+個数単位」形式
# 例: 24缶 / 60粒入 / 6本
_PACK_UNIT_RE = re.compile(r"(\d+)\s*" + _PACK_UNIT)

# 優先度 4: 「数字+入(り)」単独形式
# 例: 10入 / 30入り
_PACK_ONLY_RE = re.compile(r"(\d+)\s*入り?(?!\s*\d)")

# ── ロットパターン ─────────────────────────────
_LOT_RE = re.compile(
    r"(\d+)\s*"
    r"(?:ケース|箱(?:セット|買い)?|回分)",
)


# ══════════════════════════════════════════════
# 個別抽出ロジック
# ══════════════════════════════════════════════

def _extract_capacity_ml(normalized: str) -> Optional[float]:
    """
    正規化済みテキストから容量（mL換算）を抽出する。

    Examples:
        >>> _extract_capacity_ml("350mlx24缶")
        350.0
        >>> _extract_capacity_ml("1.5lペットボトルx6本")
        1500.0
        >>> _extract_capacity_ml("プロテイン 1kg")
        None  # 重量系は格納しない
    """
    m = _VOLUME_RE.search(normalized)
    if not m:
        return None

    value = float(m.group(1))
    unit  = m.group(2)

    if unit in ("ml", "cc"):
        return value
    if unit == "l":
        return value * 1000.0
    # g / kg / mg は重量系 → capacity_ml には格納しない
    return None


def _extract_quantity(normalized: str) -> Optional[int]:
    """
    正規化済みテキストから入数を抽出する。

    優先度:
        1. 容量+x数字 形式（350mlx24、500mlx48本）← 最重要
        2. x数字 単独形式
        3. 数字+個数単位 形式
        4. 数字+入(り) 単独形式

    Examples:
        >>> _extract_quantity("コカ・コーラ 350mlx24缶")
        24
        >>> _extract_quantity("500mlx48本")
        48
        >>> _extract_quantity("x6")
        6
        >>> _extract_quantity("60粒入")
        60
    """
    # 優先度 1: 容量+x数字（最も信頼性が高い）
    m = _VOLUME_PACK_RE.search(normalized)
    if m:
        return int(m.group(1))

    # 優先度 2: x数字 単独
    m = _PACK_X_RE.search(normalized)
    if m:
        return int(m.group(1))

    # 優先度 3: 数字+個数単位
    m = _PACK_UNIT_RE.search(normalized)
    if m:
        return int(m.group(1))

    # 優先度 4: 数字+入(り)
    m = _PACK_ONLY_RE.search(normalized)
    if m:
        return int(m.group(1))

    return None


def _extract_lot(normalized: str) -> Optional[int]:
    """
    正規化済みテキストからロット数を抽出する。

    Examples:
        >>> _extract_lot("350mlx24缶 2ケース")
        2
        >>> _extract_lot("3箱セット")
        3
        >>> _extract_lot("30回分")
        30
        >>> _extract_lot("350mlx24缶")
        None
    """
    m = _LOT_RE.search(normalized)
    if m:
        return int(m.group(1))
    return None


# ══════════════════════════════════════════════
# RawItem → ParsedItem 変換
# ══════════════════════════════════════════════

def _parse_single(raw: RawItem) -> ParsedItem:
    normalized  = _normalize(raw.raw_name)
    capacity_ml = _extract_capacity_ml(normalized)
    quantity    = _extract_quantity(normalized)
    lot         = _extract_lot(normalized)

    logger.debug(
        "regex_parser: '%s' → capacity_ml=%s, quantity=%s, lot=%s",
        raw.raw_name[:40], capacity_ml, quantity, lot,
    )

    return ParsedItem(
        mall            = raw.mall,
        item_id         = raw.item_id,
        url             = raw.url,
        raw_name        = raw.raw_name,
        price           = raw.price,
        shipping_fee    = raw.shipping_fee,
        point           = raw.point,
        coupon_discount = raw.coupon_discount,
        image_url       = raw.image_url,
        seller_name     = raw.seller_name,
        review_count    = raw.review_count,
        review_score    = raw.review_score,
        capacity_ml     = capacity_ml,
        quantity        = quantity if quantity is not None else 1,
        lot             = lot if lot is not None else 1,
        parsed_by       = "regex",
    )


# ══════════════════════════════════════════════
# 公開インターフェース
# ══════════════════════════════════════════════

def parse_with_regex(raw_item: RawItem) -> ParsedItem:
    """RawItem 1件を正規表現で解析して ParsedItem を返す。"""
    return _parse_single(raw_item)


def parse_items_with_regex(raw_items: list[RawItem]) -> list[ParsedItem]:
    """RawItem のリスト全件に正規表現パーサーを適用するバッチ処理。"""
    results = [_parse_single(raw) for raw in raw_items]

    extracted = sum(
        1 for r in results
        if r.capacity_ml is not None or r.quantity > 1 or r.lot > 1
    )
    logger.info(
        "parse_items_with_regex 完了: %d件処理, %d件で有効値抽出",
        len(results), extracted,
    )
    return results
