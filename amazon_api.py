"""
amazon_api.py (完全本番稼働版・本物API通信対応)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from pyodide.http import pyfetch
from app.models.schemas import MallType, RawItem

logger = logging.getLogger(__name__)

_PA_API_HOST       = "webservices.amazon.co.jp"
_PA_API_REGION     = "us-east-1"
_PA_API_PATH       = "/paapi5/searchitems"
_PA_API_SERVICE    = "ProductAdvertisingAPI"
_PA_API_TARGET     = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems"
_PA_API_CONTENT_TYPE = "application/json; charset=utf-8"
_MARKETPLACE       = "www.amazon.co.jp"
_MAX_ITEMS_PER_REQ = 10

_RESOURCES = [
    "ItemInfo.Title", "Offers.Listings.Price", "Offers.Listings.SavingBasis",
    "Offers.Listings.Promotions", "Offers.Listings.MerchantInfo",
    "Offers.Listings.DeliveryInfo.IsAmazonFulfilled", "Images.Primary.Medium",
    "CustomerReviews.Count", "CustomerReviews.StarRating",
]

def _load_credentials(env) -> tuple[str, str, str]:
    # Cloudflareの金庫（Secret）から本物の鍵を取り出す！
    access_key  = getattr(env, "AMAZON_ACCESS_KEY", "")
    secret_key  = getattr(env, "AMAZON_SECRET_KEY", "")
    partner_tag = getattr(env, "AMAZON_PARTNER_TAG", "")

    if not access_key or not secret_key or not partner_tag:
        raise EnvironmentError("APIキーまたはタグが未設定です")
    return access_key, secret_key, partner_tag

def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

def _derive_signing_key(secret_key: str, date_stamp: str) -> bytes:
    k_date    = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    k_region  = _hmac_sha256(k_date,    _PA_API_REGION)
    k_service = _hmac_sha256(k_region,  _PA_API_SERVICE)
    k_signing = _hmac_sha256(k_service, "aws4_request")
    return k_signing

def _build_authorization_header(access_key: str, secret_key: str, payload_json: str, amz_date: str, date_stamp: str) -> str:
    payload_bytes = payload_json.encode("utf-8")
    payload_hash  = _sha256_hex(payload_bytes)
    canonical_headers = (f"content-type:{_PA_API_CONTENT_TYPE}\n" f"host:{_PA_API_HOST}\n" f"x-amz-date:{amz_date}\n" f"x-amz-target:{_PA_API_TARGET}\n")
    signed_headers = "content-type;host;x-amz-date;x-amz-target"
    canonical_request = "\n".join(["POST", _PA_API_PATH, "", canonical_headers, signed_headers, payload_hash])
    credential_scope = f"{date_stamp}/{_PA_API_REGION}/{_PA_API_SERVICE}/aws4_request"
    string_to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, credential_scope, _sha256_hex(canonical_request.encode("utf-8"))])
    signing_key = _derive_signing_key(secret_key, date_stamp)
    signature   = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, SignedHeaders={signed_headers}, Signature={signature}"

def _build_request_headers(access_key: str, secret_key: str, payload_json: str) -> dict[str, str]:
    now = datetime.now(tz=timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    authorization = _build_authorization_header(access_key, secret_key, payload_json, amz_date, date_stamp)
    return {"Authorization":  authorization, "Content-Type": _PA_API_CONTENT_TYPE, "Host": _PA_API_HOST, "x-amz-date": amz_date, "x-amz-target": _PA_API_TARGET}

def _build_search_payload(keyword: str, partner_tag: str, item_count: int, item_page: int = 1) -> str:
    payload = {"Keywords": keyword, "Resources": _RESOURCES, "SearchIndex": "All", "Marketplace": _MARKETPLACE, "PartnerTag": partner_tag, "PartnerType": "Associates", "ItemCount": item_count, "ItemPage": item_page, "Condition": "New", "SortBy": "Relevance"}
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def _extract_price(listing: dict[str, Any]) -> int:
    try:
        amount = listing["Price"]["Amount"]
        return int(round(float(amount)))
    except (KeyError, TypeError, ValueError):
        return 0

def _extract_coupon_discount(listing: dict[str, Any], price: int) -> int:
    saving_basis = listing.get("SavingBasis", {})
    if saving_basis:
        try:
            basis_amount = int(round(float(saving_basis["Amount"])))
            discount = basis_amount - price
            if discount > 0: return discount
        except (KeyError, TypeError, ValueError): pass
    promotions = listing.get("Promotions", [])
    if promotions:
        try:
            max_discount = 0
            for promo in promotions:
                discount_pct = promo.get("DiscountPercent")
                if discount_pct is not None:
                    disc = int(round(price * float(discount_pct) / 100))
                    max_discount = max(max_discount, disc)
            if max_discount > 0: return max_discount
        except (TypeError, ValueError): pass
    return 0

def _parse_item(item: dict[str, Any], partner_tag: str) -> RawItem | None:
    asin = item.get("ASIN")
    if not asin: return None
    try: raw_name = item["ItemInfo"]["Title"]["DisplayValue"]
    except (KeyError, TypeError): return None
    
    listings = item.get("Offers", {}).get("Listings", [])
    if not listings: return None
    listing = listings[0]
    price = _extract_price(listing)
    if price <= 0: return None
    coupon_discount = _extract_coupon_discount(listing, price)
    raw_url = item.get("DetailPageURL", f"https://www.amazon.co.jp/dp/{asin}")
    
    seller_name, image_url, review_count, review_score = None, None, None, None
    try: seller_name = listing["MerchantInfo"]["Name"]
    except: pass
    try: image_url = item["Images"]["Primary"]["Medium"]["URL"]
    except: pass
    try:
        cr = item["CustomerReviews"]
        review_count = int(cr["Count"])
        review_score = float(cr["StarRating"]["Value"])
    except: pass

    return RawItem(
        mall=MallType.AMAZON, item_id=asin, url=raw_url, raw_name=raw_name,
        price=price, shipping_fee=0, point=0.0, coupon_discount=coupon_discount,
        image_url=image_url, seller_name=seller_name, review_count=review_count, review_score=review_score
    )

async def _request_search_items(access_key: str, secret_key: str, partner_tag: str, keyword: str, item_count: int, item_page: int = 1) -> dict[str, Any]:
    payload_json = _build_search_payload(keyword, partner_tag, item_count, item_page)
    headers = _build_request_headers(access_key, secret_key, payload_json)
    url = f"https://{_PA_API_HOST}{_PA_API_PATH}"
    response = await pyfetch(url, method="POST", headers=headers, body=payload_json)
    if not response.ok:
        body_text = await response.string()
        raise Exception(f"PA-API v5 エラー: status={response.status} body={body_text[:500]}")
    return await response.json()

async def fetch_amazon_items(keyword: str, env, limit: int = 10) -> list[RawItem]:
    try:
        access_key, secret_key, partner_tag = _load_credentials(env)
    except EnvironmentError as exc:
        print(f"APIキー未設定エラー: {exc}")
        return []

    results: list[RawItem] = []
    remaining: int = limit
    page: int = 1
    max_page: int = 10

    while remaining > 0 and page <= max_page:
        item_count = min(remaining, _MAX_ITEMS_PER_REQ)
        try:
            response_json = await _request_search_items(
                access_key=access_key, secret_key=secret_key, partner_tag=partner_tag,
                keyword=keyword, item_count=item_count, item_page=page
            )
        except Exception as exc:
            print(f"PA-API v5 リクエストエラー: {exc}")
            break

        items_raw = response_json.get("SearchResult", {}).get("Items", [])
        if not items_raw:
            break

        for item_raw in items_raw:
            raw_item = _parse_item(item_raw, partner_tag)
            if raw_item is not None:
                results.append(raw_item)

        remaining -= item_count
        page += 1
        total_result_count = response_json.get("SearchResult", {}).get("TotalResultCount", 0)
        if len(results) >= total_result_count:
            break

    return results