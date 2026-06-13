"""
regex_parser.py
===============
【フェーズA - モジュール4】
商品名（raw_name）から Python 標準の re モジュールのみを使い、
「容量（capacity_ml）・入数（quantity）・ロット（lot）」を高速抽出する。

【設計方針】
- 外部ライブラリ・NLP ライブラリは一切不使用（Cloudflare Workers 対応）
- 抽出できなかった項目は None / デフォルト値のままにしておく
  → 後続の ai_parser（Gemini）が「補完すべきデータ」と認識できるようにする
- parsed_by = "regex"（正規表現で処理済みのマーカー）

【正規化処理の流れ】
  商品名原文
    ↓ unicodedata.normalize("NFKC")  全角数字・全角英字を半角に一括変換
    ↓ .lower()                       大文字英字を小文字に統一
    ↓ 日本語単位の置換               ミリリットル→ml, リットル→l, etc.
    ↓ 掛け算記号の統一               × → x
  正規化済みテキスト → 各パターンマッチへ

【抽出ルールと優先順位】

  ■ 容量（capacity_ml）
    対象単位 : ml / cc / l / g / kg / mg
    抽出後変換:
      ml / cc → そのまま（float, mL 換算）
      l       → × 1000（mL 換算）
      g / kg / mg は "重量系" として capacity_ml には格納しない
             ※ g系は将来拡張フィールドへ（現行 schemas は ml のみ）
    数値形式 : 整数 / 小数（例: 1.5l → 1500ml）
    先頭優先 : 複数マッチ時は最初にヒットしたもの（商品名の先頭側）

  ■ 入数（quantity）
    優先度 1: 「x数字」形式（正規化後の掛け算表記）
    優先度 2: 「数字+個数単位」形式
              単位: 本/缶/袋/個/枚/包/粒/錠/食/杯/組/ポーチ/ピース/パック/セット
              + 任意の「分/入/入り」
    優先度 3: 「数字+入(り)」の単独表記
    抽出できなかった場合: None（ai_parser への委譲マーカー）

  ■ ロット（lot）
    ロット専用キーワード: ケース / 箱（セット）/ 回分
    入数との競合回避:
      "袋" 単体は入数側、"ケース" はロット側、"箱セット" はロット側
    抽出できなかった場合: None（ai_parser への委譲マーカー）

【ParsedItem への格納ルール】
  capacity_ml : 抽出できた場合のみ float 値。できなければ None
  quantity    : 抽出できた場合のみ int 値。できなければ None（schemas の ge=1 があるが
                スケルトンでは Optional を許容。ai_parser で補完後に確定）
  lot         : 抽出できた場合のみ int 値。できなければ None
  parsed_by   : "regex"（後段 ai_parser が "ai" に上書きする可能性あり）
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from app.models.schemas import MallType, ParsedItem, RawItem

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════
# 正規化
# ══════════════════════════════════════════════

# 日本語単位 → ASCII 単位 の置換テーブル（優先度順）
_JP_UNIT_REPLACEMENTS: list[tuple[str, str]] = [
    ("ミリリットル", "ml"),
    ("ミリリッター", "ml"),
    ("リットル",     "l"),
    ("リッター",     "l"),
    ("キログラム",   "kg"),
    ("ミリグラム",   "mg"),   # mg より先に処理（グラムより長い）
    ("グラム",       "g"),
    ("×",           "x"),    # 掛け算記号を ASCII x に統一
]


def _normalize(text: str) -> str:
    """
    商品名を正規表現マッチ用に正規化する。

    処理内容:
        1. NFKC 正規化（全角数字・全角英字・全角記号を半角に一括変換）
        2. 小文字化（ml / ML / mL を統一）
        3. 日本語単位をASCII単位に置換（ミリリットル→ml 等）
        4. 掛け算記号の統一（× → x）

    Args:
        text: 商品名の原文

    Returns:
        正規化済みテキスト

    Examples:
        >>> _normalize("アサヒ スーパードライ ３５０ｍｌ×２４缶")
        'アサヒ スーパードライ 350mlx24缶'
        >>> _normalize("シャンプー 詰替 ４５０ミリリットル×3個入")
        'シャンプー 詰替 450mlx3個入'
        >>> _normalize("１．５Ｌペットボトル×6本 2ケース")
        '1.5lペットボトルx6本 2ケース'
    """
    # Step 1: NFKC正規化（全角→半角、合成文字の正規化）
    text = unicodedata.normalize("NFKC", text)
    # Step 2: 小文字化
    text = text.lower()
    # Step 3 & 4: 日本語単位・掛け算記号の置換
    for jp, ascii_ in _JP_UNIT_REPLACEMENTS:
        text = text.replace(jp, ascii_)
    return text


# ══════════════════════════════════════════════
# コンパイル済み正規表現パターン
# ══════════════════════════════════════════════

# ── 容量パターン ──────────────────────────────
# マッチグループ:
#   group(1): 数値部分（整数 or 小数）
#   group(2): 単位（ml / cc / l / g / kg / mg）
#
# 注意事項:
#   - "l" は "lot" / "lemon" 等に誤マッチしないよう単語境界を考慮
#     （小文字化後なので大文字 L の心配不要）
#   - "g" は "greet" 等の英単語に含まれないよう後続文字を制限
#   - 各単位間で優先度は同列（最初のマッチを採用する）
_VOLUME_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(ml|cc|l(?![a-wyz])|g(?![a-z])|kg|mg)",
    # l の後続除外: [a-wyz]（lot/level/lemon 等の誤マッチを防ぐ）
    # 'x' は除外しない（"2lx6本" のような掛け算表記に対応するため）
)

# ── 入数パターン ──────────────────────────────
# 個数単位: 本/缶/袋/個/枚/包/粒/錠/食/杯/組/ポーチ/ピース/パック/セット
# + 任意の "分" "入" "入り"
_PACK_UNIT = (
    r"(?:本|缶|袋|個|枚|包|粒|錠|食|杯|組|日|ポーチ|ピース|パック|セット)"
    r"(?:分|入り?|組)?"
)

# 優先度 1: x<数字> 形式（例: x24, x6）
_PACK_X_RE = re.compile(r"x\s*(\d+)")

# 優先度 2: <数字><個数単位> 形式（例: 24缶, 60粒入）
_PACK_UNIT_RE = re.compile(r"(\d+)\s*" + _PACK_UNIT)

# 優先度 3: <数字>入(り) 単独形式（例: 10入, 30入り）
_PACK_ONLY_RE = re.compile(r"(\d+)\s*入り?(?!\s*\d)")

# ── ロットパターン ─────────────────────────────
# ロット専用キーワード: ケース / 箱セット / 回分
# "箱" 単独は入数と競合しやすいため "箱セット" / "箱買い" のみロット扱い
_LOT_RE = re.compile(
    r"(\d+)\s*"
    r"(?:ケース|箱(?:セット|買い)?|回分)",
)


# ══════════════════════════════════════════════
# 個別抽出ロジック
# ══════════════════════════════════════════════

def _extract_capacity_ml(normalized: str) -> Optional[float]:
    """
    正規化済みテキストから容量（mL 換算）を抽出する。

    単位変換ルール:
        ml / cc → そのまま（ml）
        l       → × 1000（ml）
        g / kg / mg → 重量系のため None（ml フィールドには格納しない）

    複数マッチがある場合は最初のマッチ（商品名の先頭側）を採用する。

    Args:
        normalized: _normalize() 済みのテキスト

    Returns:
        容量（float, mL 単位）または None

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
        1. x<数字> 形式（掛け算表記）
        2. <数字><個数単位> 形式
        3. <数字>入(り) 単独形式

    Args:
        normalized: _normalize() 済みのテキスト

    Returns:
        入数（int）または None

    Examples:
        >>> _extract_quantity("350mlx24缶")
        24
        >>> _extract_quantity("60粒入")
        60
        >>> _extract_quantity("プロテイン 1kg")
        None
    """
    # 優先度 1: x<数字>
    m = _PACK_X_RE.search(normalized)
    if m:
        return int(m.group(1))

    # 優先度 2: <数字><個数単位>
    m = _PACK_UNIT_RE.search(normalized)
    if m:
        return int(m.group(1))

    # 優先度 3: <数字>入(り)
    m = _PACK_ONLY_RE.search(normalized)
    if m:
        return int(m.group(1))

    return None


def _extract_lot(normalized: str) -> Optional[int]:
    """
    正規化済みテキストからロット数を抽出する。

    ロット専用キーワード（ケース / 箱セット / 回分）にマッチする最初の数値を返す。

    Args:
        normalized: _normalize() 済みのテキスト

    Returns:
        ロット数（int）または None

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
    """
    RawItem 1件を正規表現で解析し ParsedItem に変換する。

    抽出できなかったフィールドは None のままにし、
    後続の ai_parser が「補完すべきデータ」と認識できるようにする。

    Args:
        raw: モールAPIから取得した生アイテム

    Returns:
        容量・入数・ロットを可能な範囲で埋めた ParsedItem
        （parsed_by = "regex"）
    """
    normalized = _normalize(raw.raw_name)

    capacity_ml = _extract_capacity_ml(normalized)
    quantity    = _extract_quantity(normalized)
    lot         = _extract_lot(normalized)

    logger.debug(
        "regex_parser: '%s' → capacity_ml=%s, quantity=%s, lot=%s",
        raw.raw_name[:40], capacity_ml, quantity, lot,
    )

    return ParsedItem(
        # RawItem フィールドをそのまま引き継ぎ
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
        # 今回抽出したフィールド
        capacity_ml     = capacity_ml,      # 抽出できなければ None
        quantity        = quantity if quantity is not None else 1,
        lot             = lot if lot is not None else 1,
        parsed_by       = "regex",
    )


# ══════════════════════════════════════════════
# 公開インターフェース
# ══════════════════════════════════════════════

def parse_with_regex(raw_item: RawItem) -> ParsedItem:
    """
    RawItem 1件を正規表現で解析して ParsedItem を返す。

    Args:
        raw_item: モールAPIから取得した生アイテム

    Returns:
        正規表現で解析可能な範囲を埋めた ParsedItem
    """
    return _parse_single(raw_item)


def parse_items_with_regex(raw_items: list[RawItem]) -> list[ParsedItem]:
    """
    RawItem のリスト全件に正規表現パーサーを適用するバッチ処理。

    同期処理（CPU バウンド）のため非同期処理は不要。
    Cloudflare Workers の軽量環境でも高速に動作する。

    Args:
        raw_items: 全モール（Amazon / 楽天 / Yahoo）の生アイテムリスト

    Returns:
        ParsedItem のリスト（順序は入力と同一）

    Note:
        返却された ParsedItem のうち、以下の条件に当てはまるものは
        後続の ai_parser（Gemini）での補完対象となる:
            - quantity == 1 かつ 商品名に入数情報が含まれていそうな場合
            - lot == 1 かつ ロット情報が含まれていそうな場合
            - capacity_ml is None かつ容量情報が含まれていそうな場合
        ai_parser はこれらの「初期値のまま」の項目を補完し parsed_by を "ai" に更新する。
    """
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
