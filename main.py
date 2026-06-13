"""
main.py - 診断用最小実装
import を1つずつ追加して問題のモジュールを特定する
"""
from __future__ import annotations
import json

from workers import Response

# ── ここから1行ずつコメントアウトを外して問題箇所を特定 ──
from app.models.schemas import MallType          # ← Step1
from app.modules.regex_parser import parse_items_with_regex   # ← Step2
from app.modules.calculator import calculate_all  # ← Step3
from app.modules.sorter import sort_by_unit_price # ← Step4
from app.modules.affiliate_recomposer import recompose_affiliate_urls
from app.models.schemas import affiliate_item_to_dict # ← Step5
from app.modules.amazon_api import fetch_amazon_items   # ← Step6
from app.modules.rakuten_api import fetch_rakuten_items # ← Step7
from app.modules.yahoo_api import fetch_yahoo_items     # ← Step8
from app.modules.ai_parser import parse_items_with_ai   # ← Step9

async def on_fetch(request, env) -> Response:
    return Response(
        json.dumps({"status": "ok", "step": "base_import_only"}),
        status=200,
        headers={"Content-Type": "application/json"},
    )