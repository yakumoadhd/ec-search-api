# ECモール最安値検索・比較API

## プロジェクト構成

```
ec_price_search/
├── main.py                         # 司令塔・FastAPIエントリーポイント
├── app/
│   ├── models/
│   │   ├── __init__.py
│   │   └── schemas.py              # 全モジュール共通Pydanticモデル
│   └── modules/
│       ├── __init__.py
│       ├── amazon_api.py           # [フェーズA-1] Amazon商品取得
│       ├── rakuten_api.py          # [フェーズA-2] 楽天商品取得
│       ├── yahoo_api.py            # [フェーズA-3] Yahoo商品取得
│       ├── regex_parser.py         # [フェーズA-4] 正規表現パーサー
│       ├── ai_parser.py            # [フェーズA-5] Gemini AIパーサー
│       ├── calculator.py           # [フェーズB-6] 1個あたり価格計算
│       ├── sorter.py               # [フェーズB-7] 最安値順ソート
│       └── affiliate_recomposer.py # [フェーズB-8] アフィリエイトURL合成
└── README.md
```

## データフロー概要

```
keyword
  │
  ├─[並列]─ amazon_api  ─┐
  ├─[並列]─ rakuten_api ─┼─ list[RawItem]
  └─[並列]─ yahoo_api   ─┘
                          │
                    regex_parser ─ list[ParsedItem]  (正規表現で抽出)
                          │
                      ai_parser ─ list[ParsedItem]  (AI補完)
                          │
                     calculator ─ list[PricedItem]  (単価算出)
                          │
                       sorter   ─ list[PricedItem]  (最安値順)
                          │
              affiliate_recomposer ─ list[AffiliateItem]  (URL合成+rank付番)
                          │
                   SearchResponse  →  クライアント
```

## 共通データモデル

| モデル名 | 用途 |
|---|---|
| `RawItem` | モールAPIから取得した生データ |
| `ParsedItem` | 容量・入数・ロット抽出済みデータ |
| `UnitPrice` | 1個あたり価格（整数部/小数部分離） |
| `PricedItem` | 実質単価計算済みデータ |
| `AffiliateItem` | アフィリエイトURL・rank付きの最終出力 |
| `SearchRequest` | POST /search リクエストボディ |
| `SearchResponse` | POST /search レスポンスボディ |
