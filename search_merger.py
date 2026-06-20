"""search_merger.py
SearXNG 複数エンドポイントの結果をマージ・重複除去・スコアリング
Step 2-6 対応
"""

from urllib.parse import urlparse
from typing import List, Dict, Any


def _normalize_url(url: str) -> str:
    """URLを正規化（クエリストリング・トレイリングスラッシュを除去）"""
    try:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
    except Exception:
        return url


def merge_results(raw_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    複数SearXNGエンドポイントの結果をマージ。
    
    - URLで重複除去
    - 複数エンドポイントに登場した結果はスコアを加算（信頼性向上）
    - 最終的にスコア順でソート
    
    Args:
        raw_results: search_all_sync() の戻り値
                     [{"source": "HuggingFace", "data": {...}}, ...]
    
    Returns:
        マージ済み結果リスト
    """
    seen: Dict[str, Dict] = {}  # 正規化URL -> 結果アイテム

    for source_result in raw_results:
        source_name = source_result.get("source", "unknown")
        items = source_result.get("data", {}).get("results", [])

        for item in items:
            url = item.get("url", "")
            if not url:
                continue

            norm_url = _normalize_url(url)

            if norm_url in seen:
                # 重複発見：スコアを加算して信頼性を上げる
                seen[norm_url]["score"] = seen[norm_url].get("score", 1) + 1
                seen[norm_url]["sources"].append(source_name)
            else:
                # 新規登録
                seen[norm_url] = {
                    **item,
                    "score": item.get("score", 1),
                    "sources": [source_name],
                }

    # スコア順でソート
    merged = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    return merged


def merge_and_format(raw_results: List[Dict[str, Any]], limit: int = 30) -> Dict[str, Any]:
    """
    マージしてAPIレスポンス形式に整形。
    
    Args:
        raw_results: search_all_sync() の戻り値
        limit: 返す結果数の上限（デフォルト: 30）
    
    Returns:
        {
            "results": [...],
            "total": int,
            "sources_used": ["HuggingFace", "Koyeb"]
        }
    """
    merged = merge_results(raw_results)
    sources_used = list({s for r in raw_results for s in [r.get("source", "")]})

    return {
        "results": merged[:limit],
        "total": len(merged),
        "sources_used": sources_used,
    }
