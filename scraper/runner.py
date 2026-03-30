"""
PurePick 스크래핑 통합 실행기
================================
쿠팡 + 이마트몰 스크래핑 → 식품안전나라 API 보강 → 점수 산출 → products.json 저장

사용법:
    # 특정 카테고리 수집
    python -m scraper.runner --category 가공육 --source both --max 100

    # 전체 카테고리 수집 (시간 오래 걸림)
    python -m scraper.runner --all-categories --max 50

    # 쿠팡 파트너스 링크 적용
    python -m scraper.runner --category 소세지 --affiliate-tag YOUR_TAG

환경변수:
    FOOD_SAFETY_API_KEY  - 식품안전나라 API 키
    HACCP_API_KEY        - 공공데이터포털 HACCP API 키
    COUPANG_AFFILIATE_ID - 쿠팡 파트너스 ID
"""

import asyncio
import json
import logging
import os
import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from scraper.coupang_scraper import CoupangScraper, COUPANG_CATEGORY_MAP
from scraper.emart_scraper import EmartScraper, EMART_CATEGORY_MAP
from scraper.food_safety_api import FoodSafetyAPIClient, enrich_product_with_official_data
from scoring.score_engine import compute_total_score, rank_products

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

PRODUCTS_JSON = DATA_DIR / "products.json"
RAW_DIR = DATA_DIR / "raw"
RAW_DIR.mkdir(exist_ok=True)

# 쿠팡 파트너스 링크 변환
COUPANG_AFFILIATE_ID = os.environ.get("COUPANG_AFFILIATE_ID", "")


def make_coupang_affiliate_url(product_url: str, affiliate_id: str = COUPANG_AFFILIATE_ID) -> str:
    """
    쿠팡 상품 URL을 파트너스 링크로 변환.
    파트너스 링크 형식: https://link.coupang.com/a/{short_id}
    직접 API 없이는 단축 불가 → 상품 ID 기반 리다이렉트 URL 사용.
    """
    if not affiliate_id:
        return product_url

    # 쿠팡 파트너스 트래킹 파라미터 추가
    m = re.search(r"/vp/products/(\d+)", product_url)
    if not m:
        return product_url

    product_id = m.group(1)
    return (
        f"https://www.coupang.com/vp/products/{product_id}"
        f"?sourceType=affiliate&affiliate_id={affiliate_id}"
        f"&clickId={hashlib.md5(product_id.encode()).hexdigest()[:8]}"
    )


def normalize_product(raw: dict, source: str) -> dict:
    """
    스크래퍼별 원시 데이터를 통합 스키마로 정규화.
    통합 스키마:
        id, name, brand, category, price, image_url,
        coupang_url, emart_url, barcode,
        manufacturer, origin, weight,
        main_ingredient, main_ingredient_pct, additives_raw,
        certifications, haccp, iso_food,
        admin_actions, recall_count,
        scraped_at, source
    """
    normalized = {
        "id": raw.get("product_id", ""),
        "name": raw.get("name", ""),
        "brand": raw.get("brand", ""),
        "category": raw.get("category", "기타"),
        "price": raw.get("price"),
        "image_url": raw.get("image_url", ""),
        "coupang_url": "",
        "emart_url": "",
        "barcode": raw.get("barcode"),
        "manufacturer": raw.get("manufacturer", ""),
        "origin": raw.get("origin", ""),
        "weight": raw.get("weight", ""),
        "main_ingredient": raw.get("main_ingredient"),
        "main_ingredient_pct": raw.get("main_ingredient_pct"),
        "additives_raw": raw.get("additives_raw", ""),
        "raw_ingredient_text": raw.get("raw_ingredient_text", ""),
        "certifications": raw.get("certifications", []),
        "haccp": "HACCP" in raw.get("certifications", []) or raw.get("haccp", False),
        "iso_food": raw.get("iso_food", False),
        "admin_actions": raw.get("admin_actions", 0),
        "recall_count": raw.get("recall_count", 0),
        "scraped_at": raw.get("scraped_at", datetime.now().isoformat()),
        "source": source,
    }

    if source == "coupang":
        url = raw.get("coupang_url", "")
        normalized["coupang_url"] = make_coupang_affiliate_url(url) if url else ""
    elif source == "emart":
        normalized["emart_url"] = raw.get("ssg_url", "")

    return normalized


def merge_duplicate_products(products: list[dict]) -> list[dict]:
    """
    바코드 또는 (이름 + 브랜드) 기준으로 중복 제품 병합.
    쿠팡과 이마트에 동일 제품이 있을 경우 쿠팡 URL과 이마트 URL 모두 보존.
    """
    seen: dict[str, dict] = {}
    result = []

    for p in products:
        key = p.get("barcode") or f"{p.get('brand', '')}__{p.get('name', '')}"

        if key and key in seen:
            existing = seen[key]
            # URL 병합
            if p.get("coupang_url") and not existing.get("coupang_url"):
                existing["coupang_url"] = p["coupang_url"]
            if p.get("emart_url") and not existing.get("emart_url"):
                existing["emart_url"] = p["emart_url"]
            # 원재료 정보 보강 (더 상세한 쪽 사용)
            if len(p.get("raw_ingredient_text", "")) > len(existing.get("raw_ingredient_text", "")):
                existing["raw_ingredient_text"] = p["raw_ingredient_text"]
                existing["main_ingredient"] = p["main_ingredient"]
                existing["main_ingredient_pct"] = p["main_ingredient_pct"]
                existing["additives_raw"] = p["additives_raw"]
            # 인증 합산
            for cert in p.get("certifications", []):
                if cert not in existing.get("certifications", []):
                    existing.setdefault("certifications", []).append(cert)
            existing["source"] = "both"
        else:
            seen[key] = p
            result.append(p)

    return result


async def run_pipeline(
    categories: list[str],
    sources: list[str],         # ["coupang", "emart", "both"]
    max_pages: int = 3,
    max_products_per_cat: int = 100,
    enrich_with_api: bool = True,
    headless: bool = True,
) -> list[dict]:
    """
    전체 데이터 수집 파이프라인 실행.

    1. 스크래핑 (쿠팡 / 이마트)
    2. 식품안전나라 API 보강 (선택)
    3. 점수 산출
    4. 순위 매김
    5. products.json 저장
    """
    all_raw = []
    use_coupang = "coupang" in sources or "both" in sources
    use_emart = "emart" in sources or "both" in sources

    # ── Step 1: 쿠팡 스크래핑 ────────────────────
    if use_coupang:
        logger.info(f"[쿠팡] {len(categories)}개 카테고리 스크래핑 시작")
        async with CoupangScraper(headless=headless) as scraper:
            for cat in categories:
                try:
                    products = await scraper.scrape_category(
                        cat,
                        max_pages=max_pages,
                        max_products=max_products_per_cat,
                    )
                    # 원시 데이터 저장
                    raw_path = RAW_DIR / f"coupang_{cat}_{datetime.now().strftime('%Y%m%d')}.json"
                    with open(raw_path, "w", encoding="utf-8") as f:
                        json.dump(products, f, ensure_ascii=False, indent=2)

                    normalized = [normalize_product(p, "coupang") for p in products]
                    all_raw.extend(normalized)
                    logger.info(f"[쿠팡] {cat}: {len(normalized)}개")
                except Exception as e:
                    logger.error(f"[쿠팡] {cat} 실패: {e}")

    # ── Step 2: 이마트 스크래핑 ──────────────────
    if use_emart:
        logger.info(f"[이마트] {len(categories)}개 카테고리 스크래핑 시작")
        async with EmartScraper(headless=headless) as scraper:
            for cat in categories:
                try:
                    products = await scraper.scrape_category(
                        cat,
                        max_pages=max_pages,
                        max_products=max_products_per_cat,
                    )
                    raw_path = RAW_DIR / f"emart_{cat}_{datetime.now().strftime('%Y%m%d')}.json"
                    with open(raw_path, "w", encoding="utf-8") as f:
                        json.dump(products, f, ensure_ascii=False, indent=2)

                    normalized = [normalize_product(p, "emart") for p in products]
                    all_raw.extend(normalized)
                    logger.info(f"[이마트] {cat}: {len(normalized)}개")
                except Exception as e:
                    logger.error(f"[이마트] {cat} 실패: {e}")

    # ── Step 3: 중복 병합 ─────────────────────────
    logger.info(f"[병합] 총 {len(all_raw)}개 → 중복 제거 중")
    merged = merge_duplicate_products(all_raw)
    logger.info(f"[병합] 중복 제거 후 {len(merged)}개")

    # ── Step 4: 식품안전나라 API 보강 ─────────────
    if enrich_with_api and FOOD_SAFETY_API_KEY != "YOUR_API_KEY_HERE":
        logger.info("[API 보강] 식품안전나라 공식 데이터 조회 중")
        async with FoodSafetyAPIClient() as client:
            for i, product in enumerate(merged, 1):
                try:
                    merged[i - 1] = await enrich_product_with_official_data(product, client)
                    if i % 10 == 0:
                        logger.info(f"[API 보강] {i}/{len(merged)} 완료")
                    await asyncio.sleep(0.3)  # API 요청 속도 제한
                except Exception as e:
                    logger.warning(f"[API 보강] {product.get('name', '')} 실패: {e}")
    else:
        logger.info("[API 보강] API 키 미설정으로 스킵 (FOOD_SAFETY_API_KEY 환경변수 설정 필요)")

    # ── Step 5: 점수 산출 ─────────────────────────
    logger.info("[점수] 종합 점수 산출 중")
    for product in merged:
        score_result = compute_total_score(product)
        product["score"] = score_result

    # ── Step 6: 순위 매김 ─────────────────────────
    merged = rank_products(merged)

    # ── Step 7: 저장 ──────────────────────────────
    output = {
        "generated_at": datetime.now().isoformat(),
        "total_count": len(merged),
        "categories": list({p["category"] for p in merged}),
        "products": merged,
    }

    with open(PRODUCTS_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"[완료] {PRODUCTS_JSON} ({len(merged)}개 제품)")
    return merged


# ── CLI ──────────────────────────────────────────
async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="PurePick 데이터 수집 파이프라인")
    parser.add_argument("--category", nargs="+", help="수집할 카테고리 목록")
    parser.add_argument("--all-categories", action="store_true", help="전체 카테고리 수집")
    parser.add_argument(
        "--source",
        choices=["coupang", "emart", "both"],
        default="both",
        help="데이터 소스",
    )
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-products", type=int, default=100)
    parser.add_argument("--no-api", action="store_true", help="식품안전나라 API 보강 스킵")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.all_categories:
        categories = list(COUPANG_CATEGORY_MAP.keys())
    elif args.category:
        categories = args.category
    else:
        # 기본: 주요 카테고리
        categories = ["가공육", "음료", "유제품", "과자", "면류"]

    sources = [args.source] if args.source != "both" else ["coupang", "emart"]

    await run_pipeline(
        categories=categories,
        sources=sources,
        max_pages=args.max_pages,
        max_products_per_cat=args.max_products,
        enrich_with_api=not args.no_api,
        headless=not args.no_headless,
    )


if __name__ == "__main__":
    asyncio.run(_main())
