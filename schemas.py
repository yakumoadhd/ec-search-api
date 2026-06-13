"""
schemas.py
==========
全モジュール共通のデータモデル定義。
Cloudflare Workers (Pyodide) 対応のため pydantic を排除し、
Python 標準の dataclass のみで実装する。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


# ──────────────────────────────────────────────
# 補助型
# ──────────────────────────────────────────────

class MallType(str, Enum):
    """対応ECモール識別子"""
    AMAZON  = "amazon"
    RAKUTEN = "rakuten"
    YAHOO   = "yahoo"


# ──────────────────────────────────────────────
# フェーズA：各モール API が返す生データ
# ──────────────────────────────────────────────

@dataclass
class RawItem:
    """各モールAPIが返す加工前の生データ"""
    mall:            MallType
    item_id:         str
    url:             str
    raw_name:        str
    price:           int
    shipping_fee:    int            = 0
    point:           float          = 0.0
    coupon_discount: int            = 0
    image_url:       Optional[str]  = None
    seller_name:     Optional[str]  = None
    review_count:    Optional[int]  = None
    review_score:    Optional[float]= None


# ──────────────────────────────────────────────
# フェーズA：パーサーが付加する解析済みデータ
# ──────────────────────────────────────────────

@dataclass
class ParsedItem:
    """regex_parser → ai_parser を経て完成する解析済みアイテム"""
    mall:            MallType
    item_id:         str
    url:             str
    raw_name:        str
    price:           int
    shipping_fee:    int
    point:           float
    coupon_discount: int
    quantity:        int            = 1
    lot:             int            = 1
    parsed_by:       str            = "none"
    capacity_ml:     Optional[float]= None
    image_url:       Optional[str]  = None
    seller_name:     Optional[str]  = None
    review_count:    Optional[int]  = None
    review_score:    Optional[float]= None


# ──────────────────────────────────────────────
# フェーズB：計算後データ
# ──────────────────────────────────────────────

@dataclass
class UnitPrice:
    """1個あたり価格の整数部と小数部を分離して保持するサブモデル"""
    integer_part: int   # 例: 165
    decimal_part: int   # 例: 83  → ¥165.83/個

    @property
    def as_float(self) -> float:
        return self.integer_part + self.decimal_part / 100


@dataclass
class PricedItem:
    """calculator が算出した「1個あたり価格」を付加したアイテム"""
    mall:            MallType
    item_id:         str
    url:             str
    raw_name:        str
    price:           int
    shipping_fee:    int
    point:           float
    coupon_discount: int
    quantity:        int
    lot:             int
    parsed_by:       str
    effective_total: int
    total_units:     int
    unit_price:      UnitPrice
    capacity_ml:     Optional[float]= None
    image_url:       Optional[str]  = None
    seller_name:     Optional[str]  = None
    review_count:    Optional[int]  = None
    review_score:    Optional[float]= None


# ──────────────────────────────────────────────
# フェーズB：アフィリエイトURL合成後の最終出力
# ──────────────────────────────────────────────

@dataclass
class AffiliateItem:
    """affiliate_recomposer がURLをアフィリエイトURLに変換した最終出力モデル"""
    mall:            MallType
    item_id:         str
    raw_name:        str
    price:           int
    shipping_fee:    int
    point:           float
    coupon_discount: int
    quantity:        int
    lot:             int
    parsed_by:       str
    effective_total: int
    total_units:     int
    unit_price:      UnitPrice
    affiliate_url:   str
    rank:            int
    capacity_ml:     Optional[float]= None
    image_url:       Optional[str]  = None
    seller_name:     Optional[str]  = None
    review_count:    Optional[int]  = None
    review_score:    Optional[float]= None


# ──────────────────────────────────────────────
# dict 変換ヘルパー（JSON シリアライズ用）
# ──────────────────────────────────────────────

def _unit_price_to_dict(up: UnitPrice) -> dict:
    return {
        "integer_part": up.integer_part,
        "decimal_part":  up.decimal_part,
        "as_float":      up.as_float,
    }

def affiliate_item_to_dict(item: AffiliateItem) -> dict:
    """AffiliateItem を JSON シリアライズ可能な dict に変換する"""
    return {
        "mall":            item.mall.value,
        "item_id":         item.item_id,
        "raw_name":        item.raw_name,
        "price":           item.price,
        "shipping_fee":    item.shipping_fee,
        "point":           item.point,
        "coupon_discount": item.coupon_discount,
        "quantity":        item.quantity,
        "lot":             item.lot,
        "parsed_by":       item.parsed_by,
        "effective_total": item.effective_total,
        "total_units":     item.total_units,
        "unit_price":      _unit_price_to_dict(item.unit_price),
        "affiliate_url":   item.affiliate_url,
        "rank":            item.rank,
        "capacity_ml":     item.capacity_ml,
        "image_url":       item.image_url,
        "seller_name":     item.seller_name,
        "review_count":    item.review_count,
        "review_score":    item.review_score,
    }