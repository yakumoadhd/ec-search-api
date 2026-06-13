"""
ai_parser.py
============
【フェーズA - モジュール5】
regex_parser で容量・入数・ロットがすべて抽出できなかったアイテムのみを
Gemini API（REST エンドポイント直接呼び出し）に投げて情報を補完する。

【設計方針】
- Google 公式 SDK（google-genai 等）は一切不使用（Cloudflare Workers 対応）
- pyodide.http.pyfetch で Gemini REST API を直接呼び出す（Cloudflare Workers ネイティブ）
- 使用モデル: gemini-3.5-flash（最新・高速・低コスト）
- Structured Outputs（responseMimeType + responseSchema）で JSON を確実に取得
- 機密情報（GEMINI_API_KEY）は環境変数から受け取る

【コスト最適化（エコ設計）の詳細】

  ■ AI 補完対象の判定ロジック（_needs_ai_parse）
    以下の条件をすべて満たす場合のみ Gemini を呼び出す:
        capacity_ml is None
        AND quantity == 1（デフォルト値）
        AND lot == 1（デフォルト値）
    = regex_parser が 3フィールドすべてをデフォルト値のまま返したアイテム

    理由:
        ・regex で何か 1フィールドでも取れていれば、
          残りフィールドは「その商品に本当に存在しないデータ」である可能性が高い
          （例: 容量は取れたが入数は単品商品、など）
        ・3フィールドすべて未取得の場合のみ「regex が手がかりを掴めなかった商品名」
          として AI に回すことで APIコストを最小化する

  ■ 並列処理による待機時間の最小化
    対象アイテムを asyncio.gather で並列送信し、逐次呼び出しより大幅に高速化

【Gemini API Structured Outputs の仕様】
  generationConfig:
    responseMimeType: "application/json"
    responseSchema:
      type: OBJECT
      properties:
        volume:     {type: NUMBER}  ← 容量（ml）。不明なら 0
        pack_count: {type: NUMBER}  ← 入数（個）。不明なら 1
        lot_count:  {type: NUMBER}  ← ロット数。不明なら 1
      required: [volume, pack_count, lot_count]
    temperature: 0               ← 決定論的出力（ブレを排除）
    maxOutputTokens: 128         ← JSON は短いので十分

【ParsedItem への書き戻しルール】
  volume > 0     → capacity_ml を上書き
  pack_count > 1 → quantity を上書き
  lot_count > 1  → lot を上書き
  parsed_by      → "ai" に更新（regex から上書き）

  ※ 値が 0 や 1 の場合は「不明」とみなしてデフォルト値を維持する
     （「1個入りの単品商品」と「入数不明」を区別できないため、
       1 はデフォルト値として保持し calculator でそのまま使用する）
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from pyodide.http import pyfetch

from app.models.schemas import ParsedItem

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Gemini API 定数
# ──────────────────────────────────────────────

_GEMINI_MODEL    = "gemini-3.5-flash"
_GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_GEMINI_TIMEOUT  = 15.0     # 秒（AI 推論はネットワーク待機が長め）

# Structured Output 用レスポンススキーマ
# NUMBER 型は float/int 両方を受け入れ、プログラム側で int 変換する
_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "volume": {
            "type":        "NUMBER",
            "description": "商品1個あたりの容量（ml 換算）。不明なら 0。",
        },
        "pack_count": {
            "type":        "NUMBER",
            "description": "1パッケージに含まれる個数（入数）。不明なら 1。",
        },
        "lot_count": {
            "type":        "NUMBER",
            "description": "まとめ買いのセット数（ロット数）。不明なら 1。",
        },
    },
    "required": ["volume", "pack_count", "lot_count"],
}

# Gemini プロンプト（テンプレート）
# {product_name} を実際の商品名に置換して使用する
_PROMPT_TEMPLATE = """\
あなたはECサイトの商品情報解析AIです。
以下の商品名から「容量」「入数」「ロット数」を抽出してください。

商品名: {product_name}

抽出ルール:
- volume    : 商品1個あたりの容量（ml 換算の数値のみ）。Lはml換算、gやkgは対象外。不明なら 0。
- pack_count: 1パッケージ・1箱・1セットに含まれる個数（入数）。不明または単品なら 1。
- lot_count : まとめ買いのケース数・箱数などロット単位の数量。不明なら 1。

注意:
- 推測が難しい場合は、安全な初期値（volume=0, pack_count=1, lot_count=1）を返してください。
- 数値のみを返し、単位や説明文は不要です。
"""


# ──────────────────────────────────────────────
# 環境変数ローダー
# ──────────────────────────────────────────────

def _load_api_key(api_key: str | None = None) -> str:
    """
    Gemini API キーを引数または環境変数から取得する。

    優先順位:
        1. 引数 api_key が None でなければそれを使用
        2. 環境変数 GEMINI_API_KEY

    Args:
        api_key: 明示的に渡す API キー（省略可）

    Returns:
        API キー文字列

    Raises:
        EnvironmentError: API キーが見つからない場合
    """
    if api_key:
        return api_key
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise EnvironmentError(
            "Gemini API キーが未設定です: 引数 api_key または環境変数 GEMINI_API_KEY を設定してください。"
        )
    return key


# ──────────────────────────────────────────────
# 補完要否の判定
# ──────────────────────────────────────────────

def _needs_ai_parse(item: ParsedItem) -> bool:
    """
    AI 補完が必要かどうかを判定する。

    判定ロジック:
        capacity_ml is None
        AND quantity == 1（デフォルト値のまま）
        AND lot == 1（デフォルト値のまま）

    = regex_parser が 3フィールドすべてをデフォルト値のまま返した = 手がかりゼロ
    この条件を満たすアイテムのみ Gemini に送信することで APIコストを最小化する。

    Args:
        item: regex_parser 処理済みの ParsedItem

    Returns:
        True → AI 補完対象 / False → スルー（そのまま返す）
    """
    return (
        item.capacity_ml is None
        and item.quantity == 1
        and item.lot == 1
    )


# ──────────────────────────────────────────────
# Gemini API リクエスト構築
# ──────────────────────────────────────────────

def _build_request_body(product_name: str) -> dict[str, Any]:
    """
    Gemini generateContent API のリクエストボディを組み立てる。

    Args:
        product_name: 解析対象の商品名

    Returns:
        JSON シリアライズ可能なリクエストボディ辞書
    """
    prompt = _PROMPT_TEMPLATE.format(product_name=product_name)

    return {
        "contents": [
            {
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema":   _RESPONSE_SCHEMA,
            "temperature":      0,      # 決定論的出力（ブレを排除）
            "maxOutputTokens":  128,    # JSON は短いので十分
        },
    }


# ──────────────────────────────────────────────
# Gemini API レスポンスのパース
# ──────────────────────────────────────────────

def _parse_gemini_response(response_json: dict[str, Any]) -> dict[str, Any] | None:
    """
    Gemini generateContent レスポンスから JSON テキストを取り出してパースする。

    レスポンス構造:
        {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "{\"volume\":350,\"pack_count\":24,\"lot_count\":1}"}]
                    }
                }
            ]
        }

    Args:
        response_json: Gemini API のレスポンス辞書

    Returns:
        パース済みの辞書 {"volume": ..., "pack_count": ..., "lot_count": ...}
        または None（パース失敗時）
    """
    try:
        text = (
            response_json["candidates"][0]["content"]["parts"][0]["text"]
        )
        parsed = json.loads(text)
        # 必須キーの存在確認
        if not all(k in parsed for k in ("volume", "pack_count", "lot_count")):
            logger.warning("Gemini レスポンスに必須キーが欠損しています: %s", parsed)
            return None
        return parsed
    except (KeyError, IndexError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Gemini レスポンスのパースに失敗しました: %s", exc)
        return None


# ──────────────────────────────────────────────
# ParsedItem への書き戻し
# ──────────────────────────────────────────────

def _apply_ai_result(item: ParsedItem, ai_result: dict[str, Any]) -> ParsedItem:
    """
    Gemini の解析結果を ParsedItem に書き戻して新しい ParsedItem を返す。

    書き戻しルール:
        volume > 0     → capacity_ml を上書き（0 は「不明」として無視）
        pack_count > 1 → quantity を上書き（1 は単品 or 不明なのでデフォルトのまま）
        lot_count > 1  → lot を上書き（1 は単個 or 不明なのでデフォルトのまま）
        parsed_by      → "ai" に更新

    dataclasses.replace を使ってイミュータブルに更新する。

    Args:
        item      : 書き戻し先の ParsedItem（変更しない）
        ai_result : _parse_gemini_response で取得した辞書

    Returns:
        更新済みの新しい ParsedItem インスタンス
    """
    updates: dict[str, Any] = {"parsed_by": "ai"}

    volume = ai_result.get("volume", 0)
    if isinstance(volume, (int, float)) and volume > 0:
        updates["capacity_ml"] = float(volume)

    pack_count = ai_result.get("pack_count", 1)
    if isinstance(pack_count, (int, float)) and int(pack_count) > 1:
        updates["quantity"] = int(pack_count)

    lot_count = ai_result.get("lot_count", 1)
    if isinstance(lot_count, (int, float)) and int(lot_count) > 1:
        updates["lot"] = int(lot_count)

    from dataclasses import replace as _dc_replace
    return _dc_replace(item, **updates)


# ──────────────────────────────────────────────
# 1件分の Gemini 呼び出しと補完
# ──────────────────────────────────────────────

async def _parse_single_with_ai(
    item:    ParsedItem,
    api_key: str,
) -> ParsedItem:
    """
    ParsedItem 1件を Gemini API に投げて情報を補完し、更新済み ParsedItem を返す。
    Cloudflare Workers ネイティブの pyfetch を使用。

    API 呼び出しが失敗した場合は元の ParsedItem をそのまま返す（フォールバック）。
    本番環境でのサービス継続性を優先し、AI 補完失敗を致命的エラーとしない。

    Args:
        item    : 補完対象の ParsedItem
        api_key : Gemini API キー

    Returns:
        補完済み ParsedItem（失敗時は元の ParsedItem）
    """
    endpoint = (
        f"{_GEMINI_API_BASE}/{_GEMINI_MODEL}:generateContent"
        f"?key={api_key}"
    )
    request_body = _build_request_body(item.raw_name)

    try:
        response = await pyfetch(
            endpoint,
            method  = "POST",
            headers = {"Content-Type": "application/json"},
            body    = json.dumps(request_body, ensure_ascii=False),
        )

        if not response.ok:
            body_text = await response.string()
            logger.error(
                "Gemini API HTTPエラー: status=%d, item='%s', body=%s",
                response.status,
                item.raw_name[:40],
                body_text[:300],
            )
            return item   # フォールバック

        response_json = await response.json()

    except Exception as exc:
        logger.error(
            "Gemini API リクエストエラー: item='%s', error=%s",
            item.raw_name[:40],
            exc,
        )
        return item   # フォールバック

    ai_result = _parse_gemini_response(response_json)
    if ai_result is None:
        logger.warning("Gemini からの有効な結果が得られませんでした: '%s'", item.raw_name[:40])
        return item   # フォールバック

    updated = _apply_ai_result(item, ai_result)

    logger.debug(
        "AI補完完了: '%s' → capacity_ml=%s, quantity=%d, lot=%d",
        item.raw_name[:40],
        updated.capacity_ml,
        updated.quantity,
        updated.lot,
    )
    return updated


# ──────────────────────────────────────────────
# 公開インターフェース
# ──────────────────────────────────────────────

async def parse_with_ai(
    parsed_item: ParsedItem,
    api_key:     str | None = None,
) -> ParsedItem:
    """
    ParsedItem 1件を Gemini API で補完して返す。

    Args:
        parsed_item: regex_parser が処理した ParsedItem
        api_key    : Gemini API キー（省略時は環境変数 GEMINI_API_KEY を使用）

    Returns:
        補完済み ParsedItem
    """
    key = _load_api_key(api_key)
    if not _needs_ai_parse(parsed_item):
        return parsed_item

    return await _parse_single_with_ai(parsed_item, key)


async def parse_items_with_ai(
    parsed_items: list[ParsedItem],
    api_key:      str | None = None,
) -> list[ParsedItem]:
    """
    ParsedItem のリストを受け取り、AI 補完が必要なアイテムのみを
    Gemini API に並列送信して補完するオーケストレーター。

    処理フロー:
        1. _needs_ai_parse でアイテムを「補完要」「スルー」に振り分ける
        2. 「補完要」アイテムを asyncio.gather で並列 Gemini 送信
        3. 結果を元の順序で結合して返す

    コスト最適化:
        - 補完不要アイテム（regex で全フィールド取得済み）は Gemini を呼ばない
        - 補完要アイテムのみを並列処理することで通信コスト・時間を最小化

    Args:
        parsed_items: regex_parser 処理済みの全アイテムリスト
        api_key     : Gemini API キー（省略時は環境変数 GEMINI_API_KEY を使用）

    Returns:
        全アイテムの補完済み ParsedItem リスト（入力と同じ順序）

    Raises:
        EnvironmentError: API キーが未設定かつ引数にも渡されていない場合
    """
    key = _load_api_key(api_key)

    # ── ステップ1: 補完要否の振り分け ──────────────────────────
    # needs[i] = True  → AI 補完が必要
    # needs[i] = False → スルー（そのまま返す）
    needs = [_needs_ai_parse(item) for item in parsed_items]

    target_items  = [item for item, n in zip(parsed_items, needs) if n]
    skipped_count = len(parsed_items) - len(target_items)

    logger.info(
        "parse_items_with_ai: 全%d件 → AI補完対象=%d件 / スキップ=%d件",
        len(parsed_items), len(target_items), skipped_count,
    )

    if not target_items:
        # 全件スキップ: API を一切呼ばずに即返却
        return parsed_items

    # ── ステップ2: 並列 Gemini 送信 ────────────────────────────
    ai_results: list[ParsedItem] = await asyncio.gather(
        *[_parse_single_with_ai(item, key) for item in target_items],
        return_exceptions=False,   # 個別エラーは _parse_single_with_ai 内でフォールバック済み
    )

    # ── ステップ3: 元の順序で結合して返す ─────────────────────
    result: list[ParsedItem] = []
    ai_iter = iter(ai_results)

    for item, needed in zip(parsed_items, needs):
        if needed:
            result.append(next(ai_iter))
        else:
            result.append(item)

    ai_updated = sum(
        1 for orig, updated in zip(parsed_items, result)
        if orig.parsed_by != updated.parsed_by
    )
    logger.info(
        "parse_items_with_ai 完了: AI補完成功=%d件 / フォールバック=%d件",
        ai_updated,
        len(target_items) - ai_updated,
    )

    return result