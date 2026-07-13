"""
ai_parser.py
============
【フェーズA - モジュール5】
regex_parser で容量・入数・ロットがすべて抽出できなかったアイテムのみを
Oracle VM上のOllama（qwen2.5:14b）に投げて情報を補完する。

【設計方針】
- Gemini API 完全排除・自前AIで完結
- Ollama REST API（/api/chat）を aiohttp で非同期呼び出し
- Structured Outputs（format スキーマ）で JSON を確実に取得
- 接続先: Tailscale経由 100.92.194.114:11434（プライベートIP・外部非公開）
- APIキー不要・永久無料・Gemini依存ゼロ

【コスト最適化（エコ設計）の詳細】

  ■ AI 補完対象の判定ロジック（_needs_ai_parse）
    以下の条件をすべて満たす場合のみ Ollama を呼び出す:
        capacity_ml is None
        AND quantity == 1（デフォルト値）
        AND lot == 1（デフォルト値）
    = regex_parser が 3フィールドすべてをデフォルト値のまま返したアイテム

  ■ 並列処理による待機時間の最小化
    対象アイテムを asyncio.gather で並列送信
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace as _dc_replace
from typing import Any

import aiohttp

from app.models.schemas import ParsedItem

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Ollama 定数
# ──────────────────────────────────────────────

_OLLAMA_HOST    = "http://100.92.194.114:11434"
_OLLAMA_MODEL   = "qwen2.5:14b"
_OLLAMA_TIMEOUT = 30.0  # 秒（ローカルLLM推論は少し余裕を持たせる）

# Structured Output 用フォーマットスキーマ（Ollama format パラメータ）
_FORMAT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "volume": {
            "type":        "number",
            "description": "商品1個あたりの容量（ml 換算）。不明なら 0。",
        },
        "pack_count": {
            "type":        "number",
            "description": "1パッケージに含まれる個数（入数）。不明なら 1。",
        },
        "lot_count": {
            "type":        "number",
            "description": "まとめ買いのセット数（ロット数）。不明なら 1。",
        },
    },
    "required": ["volume", "pack_count", "lot_count"],
}

# プロンプトテンプレート
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
# 補完要否の判定
# ──────────────────────────────────────────────

def _needs_ai_parse(item: ParsedItem) -> bool:
    return (
        item.capacity_ml is None
        and item.quantity == 1
        and item.lot == 1
    )


# ──────────────────────────────────────────────
# Ollama API リクエスト構築
# ──────────────────────────────────────────────

def _build_request_body(product_name: str) -> dict[str, Any]:
    prompt = _PROMPT_TEMPLATE.format(product_name=product_name)
    return {
        "model":    _OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream":   False,
        "format":   _FORMAT_SCHEMA,
        "options": {
            "temperature": 0,        # 決定論的出力
            "num_predict": 128,      # JSONは短いので十分
        },
    }


# ──────────────────────────────────────────────
# Ollama レスポンスのパース
# ──────────────────────────────────────────────

def _parse_ollama_response(response_json: dict[str, Any]) -> dict[str, Any] | None:
    """
    Ollama /api/chat レスポンスから JSON を取り出してパースする。

    レスポンス構造:
        {
            "message": {
                "role": "assistant",
                "content": "{\"volume\":350,\"pack_count\":24,\"lot_count\":1}"
            },
            "done": true
        }
    """
    try:
        content = response_json["message"]["content"]
        parsed  = json.loads(content)
        if not all(k in parsed for k in ("volume", "pack_count", "lot_count")):
            logger.warning("Ollama レスポンスに必須キーが欠損: %s", parsed)
            return None
        return parsed
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        logger.warning("Ollama レスポンスのパース失敗: %s", exc)
        return None


# ──────────────────────────────────────────────
# ParsedItem への書き戻し
# ──────────────────────────────────────────────

def _apply_ai_result(item: ParsedItem, ai_result: dict[str, Any]) -> ParsedItem:
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

    return _dc_replace(item, **updates)


# ──────────────────────────────────────────────
# 1件分の Ollama 呼び出しと補完
# ──────────────────────────────────────────────

async def _parse_single_with_ai(item: ParsedItem) -> ParsedItem:
    """
    ParsedItem 1件を Ollama API に投げて情報を補完する。
    失敗時は元の ParsedItem をそのまま返す（フォールバック）。
    """
    endpoint     = f"{_OLLAMA_HOST}/api/chat"
    request_body = _build_request_body(item.raw_name)

    try:
        timeout = aiohttp.ClientTimeout(total=_OLLAMA_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                endpoint,
                json=request_body,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status != 200:
                    body_text = await response.text()
                    logger.error(
                        "Ollama API HTTPエラー: status=%d, item='%s', body=%s",
                        response.status,
                        item.raw_name[:40],
                        body_text[:300],
                    )
                    return item  # フォールバック

                response_json = await response.json()

    except Exception as exc:
        logger.error(
            "Ollama API リクエストエラー: item='%s', error=%s",
            item.raw_name[:40],
            exc,
        )
        return item  # フォールバック

    ai_result = _parse_ollama_response(response_json)
    if ai_result is None:
        logger.warning("Ollama から有効な結果が得られず: '%s'", item.raw_name[:40])
        return item  # フォールバック

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
# 公開インターフェース（元と完全互換）
# ──────────────────────────────────────────────

async def parse_with_ai(
    parsed_item: ParsedItem,
    api_key:     str | None = None,  # 互換性のため残す・使用しない
) -> ParsedItem:
    if not _needs_ai_parse(parsed_item):
        return parsed_item
    return await _parse_single_with_ai(parsed_item)


async def parse_items_with_ai(
    parsed_items: list[ParsedItem],
    api_key:      str | None = None,  # 互換性のため残す・使用しない
) -> list[ParsedItem]:
    needs = [_needs_ai_parse(item) for item in parsed_items]

    target_items  = [item for item, n in zip(parsed_items, needs) if n]
    skipped_count = len(parsed_items) - len(target_items)

    logger.info(
        "parse_items_with_ai: 全%d件 → AI補完対象=%d件 / スキップ=%d件",
        len(parsed_items), len(target_items), skipped_count,
    )

    if not target_items:
        return parsed_items

    ai_results: list[ParsedItem] = await asyncio.gather(
        *[_parse_single_with_ai(item) for item in target_items],
        return_exceptions=False,
    )

    result: list[ParsedItem] = []
    ai_iter = iter(ai_results)
    for item, needed in zip(parsed_items, needs):
        result.append(next(ai_iter) if needed else item)

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
