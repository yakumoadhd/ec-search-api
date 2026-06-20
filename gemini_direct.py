"""gemini_direct.py
Step 2-5d: フロントエンドからGemini APIへ直接fetchするヘルパー関数群

OAuth2アクセストークン（ユーザーのGoogleアカウント経由）でGeminiを呼び出す。
サーバー側にAPIキーは一切保持しない。
"""

import aiohttp
import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-2.0-flash:generateContent"
)

MAX_RETRIES = 2
RETRY_WAIT_SEC = 0.5


async def call_gemini(
    access_token: str,
    prompt: str,
) -> Optional[str]:
    """
    ユーザーのOAuth2アクセストークンでGemini APIを呼び出す。

    Args:
        access_token: GISで取得したアクセストークン
        prompt: Geminiに渡すテキスト

    Returns:
        Geminiのレスポンステキスト。失敗時はNone。
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "contents": [{"parts": [{"text": prompt}]}]
    }

    for attempt in range(MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GEMINI_ENDPOINT,
                    headers=headers,
                    json=body,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        text = (
                            data.get("candidates", [{}])[0]
                            .get("content", {})
                            .get("parts", [{}])[0]
                            .get("text", "")
                        )
                        logger.info("[Gemini] 成功")
                        return text

                    elif resp.status == 429:
                        # レート制限: リトライ
                        if attempt < MAX_RETRIES:
                            logger.warning(f"[Gemini] 429 レート制限 - {RETRY_WAIT_SEC}s待機してリトライ")
                            await asyncio.sleep(RETRY_WAIT_SEC)
                            continue
                        logger.warning("[Gemini] 429 リトライ消費 - スキップ")
                        return None

                    elif resp.status == 401:
                        # トークン切れ: 再取得フローへ
                        logger.warning("[Gemini] 401 トークン切れ - フロントエンドで再取得必要")
                        return None

                    else:
                        logger.warning(f"[Gemini] HTTP {resp.status}")
                        return None

        except asyncio.TimeoutError:
            logger.warning("[Gemini] タイムアウト")
            return None
        except Exception as e:
            logger.warning(f"[Gemini] エラー: {e}")
            return None

    return None


async def analyze_product_name(
    access_token: str,
    raw_name: str,
) -> Optional[str]:
    """
    商品名をGeminiで解析し、正規化した商品名を返す。
    未ログイン / API未有効化時はNoneを返して呼び出し元で正規表現のみ处理。
    """
    prompt = f"""商品名の表記ゆれを山めて正規化してください。
ノイズ（「送料無料」「新品」「最安値」など）を除外して、
商品名のみを簡潔に1行で返してください。

入力: {raw_name}
出力（商品名のみ）:"""

    return await call_gemini(access_token, prompt)


def analyze_product_name_sync(
    access_token: str,
    raw_name: str,
) -> Optional[str]:
    """Flaskから呼び出す用の同期ラッパー"""
    return asyncio.run(analyze_product_name(access_token, raw_name))
