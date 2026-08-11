# PriceRanking - Backend API

PriceRanking（プライスランキング）のバックエンドAPIです。

## 概要

PriceRankingは、複数のECサイトの価格を比較する最安値比較アプリです。
このリポジトリはバックエンドAPI（Python FastAPI）を管理します。

## 構成ファイル

- `affiliate_recomposer.py` - アフィリエイトURL再構成
- `ai_parser.py` - AI解析（Gemini）
- `amazon_api.py` - Amazon PA-API連携
- `calculator.py` - 実質価格計算
- `searxng_client.py` - SearXNG クライアント（n8n Proxy経由）
- `search_merger.py` - SearXNG検索結果マージ処理
- `gemini_direct.py` - Gemini API直接fetchヘルパー

## 開発環境

- Amazon Fire HD 10（メイン開発端末）
- iPhone 14（Safari動作確認）
- Google AI Studio / Claude（AI支援開発）

## デプロイ

Cloud Run（Google Cloud）にデプロイ済み。
