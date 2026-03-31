"""
식품안전나라 C005 API 대량 수집기
====================================
스크래핑 없이 공식 API만으로 가공식품 데이터를 수집합니다.

사용법:
    # .env 파일에 FOOD_SAFETY_API_KEY 설정 후:
    python -m scraper.api_collector --max 1000
    python -m scraper.api_collector --category 가공육 --max 500
    python -m scraper.api_collector --all --max 5000
"""

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

FOOD_SAFETY_API_KEY = os.environ.get("FOOD_SAFETY_API_KEY", "")
BASE_URL = "https://openapi.foodsafetykorea.go.kr/api"

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
PRODUCTS_JSON = DATA_DIR / "products.json"

# 식품유형 → 카테고리 매핑
FOOD_TYPE_TO_CATEGORY = {
    "햄류": "가공육", "소시지류": "가공육", "베이컨류": "가공육",
    "가공육": "가공육", "육가공": "가공육", "식육가공": "가공육",
    "우유류": "유제품", "발효유류": "유제품", "치즈류": "유제품",
    "버터류": "유제품", "아이스크림류": "유제품",
    "탄산음료": "음료", "과채음료": "음료", "혼합음료": "음료",
    "두유류": "음료", "음료베이스": "음료", "인삼음료": "음료",
    "과자": "과자", "캔디류": "과자", "초콜릿류": "과자",
    "빙과류": "과자", "스낵": "과자", "비스킷류": "과자",
    "면류": "면류", "냉면": "면류", "파스타": "면류", "라면": "면류",
    "통조림": "통조림", "레토르트": "통조림", "병조림": "통조림",
    "냉동": "냉동식품", "냉동식품": "냉동식품",
    "소스": "소스·조미료", "드레싱": "소스·조미료", "조미료": "소스·조미료",
    "빵류": "빵·베이커리", "케이크": "빵·베이커리",
    "두부": "두부·콩나물", "콩나물": "두부·콩나물",
    "김치": "김치·반찬", "반찬": "김치·반찬",
}

# 첨가물 위험도 (점수 계산용)
HIGH_RISK_ADDITIVES = {
    "아질산나트륨", "아질산칼륨", "황색4호", "황색5호", "적색2호",
    "적색40호", "이산화티타늄", "BHA", "BHT", "아스파탐", "카라기난",
}
MED_RISK_ADDITIVES = {
    "MSG", "글루탐산나트륨", "안식향산나트륨", "사카린", "인산",
    "카라멜색소", "수크랄로스", "아세설팜칼륨",
}


def classify_category(food_type: str) -> str:
    """식품유형 → 카테고리 분류."""
    if not food_type:
        return "기타"
    for key, cat in FOOD_TYPE_TO_CATEGORY.items():
        if key in food_type:
            return cat
    return "기타"


def parse_ingredients(raw_text: str) -> dict:
    """원재료명 문자열 파싱."""
    if not raw_text:
        return {"main": None, "main_pct": None, "additives": [], "additives_raw": ""}

    m = re.search(r"^([가-힣a-zA-Z]+(?:\([^)]+\))?)\s*(\d+(?:\.\d+)?)%", raw_text.strip())
    main_name = m.group(1) if m else None
    main_pct = float(m.group(2)) if m else None

    rest = raw_text[m.end():].strip().lstrip(",·/ ") if m else raw_text
    additives = [t.strip() for t in re.split(r"[,·/；;、]+", rest) if t.strip() and len(t.strip()) > 1]

    return {"main": main_name, "main_pct": main_pct, "additives": additives, "additives_raw": rest}


def score_product(product: dict) -> dict:
    """제품 점수 계산 (0~100점)."""
    # 1. 주원료 함량 (30점)
    pct = product.get("main_ingredient_pct") or 0
    if pct >= 90:
        s1 = 30
    elif pct >= 70:
        s1 = 25
    elif pct >= 50:
        s1 = 18
    elif pct >= 30:
        s1 = 10
    else:
        s1 = 5 if pct > 0 else 10  # 함량 미표기

    # 2. 첨가물 (35점)
    additives_raw = product.get("additives_raw", "")
    s2 = 35
    for add in HIGH_RISK_ADDITIVES:
        if add in additives_raw:
            s2 -= 7
    for add in MED_RISK_ADDITIVES:
        if add in additives_raw:
            s2 -= 3
    additive_list = [a for a in product.get("additives", []) if a]
    s2 -= len(additive_list) * 0.5
    s2 = max(0, min(35, s2))

    # 3. 인증 (20점)
    certs = product.get("certifications", [])
    s3 = 10  # 기본
    if "HACCP" in certs:
        s3 += 5
    if "유기농" in certs:
        s3 += 5
    if "무항생제" in certs:
        s3 += 3
    if "무농약" in certs:
        s3 += 2
    s3 = min(20, s3)

    # 4. 위생 (15점)
    s4 = 15
    s4 -= product.get("admin_actions", 0) * 3
    s4 -= product.get("recall_count", 0) * 5
    s4 = max(0, s4)

    total = round(s1 + s2 + s3 + s4)
    if total >= 85:
        grade = "우수"
    elif total >= 70:
        grade = "양호"
    elif total >= 50:
        grade = "보통"
    else:
        grade = "주의"

    return {"total": total, "grade": grade, "ingredient": round(s1), "additives": round(s2), "certifications": s3, "hygiene": s4}


async def fetch_batch(session: aiohttp.ClientSession, api_key: str, start: int, end: int, food_type: str = "") -> list[dict]:
    """C005 API 배치 조회."""
    url = f"{BASE_URL}/{api_key}/C005/json/{start}/{end}"
    if food_type:
        url += f"/PRDLST_DCNM={food_type}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                return []
            data = await resp.json(content_type=None)
            return data.get("C005", {}).get("row", [])
    except Exception as e:
        logger.warning(f"배치 {start}-{end} 실패: {e}")
        return []


def row_to_product(r: dict) -> dict:
    """API 응답 row → 제품 딕셔너리 변환."""
    raw_ingredients = r.get("RAWMTRL_NM", "") or ""
    parsed = parse_ingredients(raw_ingredients)

    certs = []
    combined = " ".join(str(v) for v in r.values() if v)
    if "HACCP" in combined or "해썹" in combined:
        certs.append("HACCP")
    if "유기농" in combined:
        certs.append("유기농")
    if "무항생제" in combined:
        certs.append("무항생제")
    if "무농약" in combined:
        certs.append("무농약")

    product = {
        "id": r.get("SNACK_NM", "") + "_" + r.get("BSSH_NM", ""),
        "name": r.get("PRDLST_NM") or r.get("SNACK_NM", ""),
        "brand": r.get("BSSH_NM", ""),
        "category": classify_category(r.get("PRDLST_DCNM", "")),
        "price": None,
        "image_url": "",
        "coupang_url": "",
        "emart_url": "",
        "barcode": r.get("BAR_CD", ""),
        "manufacturer": r.get("BSSH_NM", ""),
        "origin": r.get("ORPLC_INFO", ""),
        "weight": r.get("SERVING_SIZE", "") or r.get("CAPACITY", ""),
        "main_ingredient": parsed["main"],
        "main_ingredient_pct": parsed["main_pct"],
        "additives_raw": parsed["additives_raw"],
        "additives": parsed["additives"],
        "raw_ingredient_text": raw_ingredients,
        "certifications": certs,
        "haccp": "HACCP" in certs,
        "iso_food": False,
        "admin_actions": 0,
        "recall_count": 0,
        "scraped_at": datetime.now().isoformat(),
        "source": "food_safety_api",
    }
    score = score_product(product)
    product["score"] = score
    return product


async def collect_all(api_key: str, max_products: int = 1000, batch_size: int = 100) -> list[dict]:
    """C005 API 전체 대량 수집."""
    all_products = []
    logger.info(f"[API 수집] 최대 {max_products}개 수집 시작")

    async with aiohttp.ClientSession() as session:
        for start in range(1, max_products + 1, batch_size):
            end = min(start + batch_size - 1, max_products)
            rows = await fetch_batch(session, api_key, start, end)
            if not rows:
                logger.info(f"[API 수집] {start}번에서 데이터 없음, 종료")
                break

            for r in rows:
                p = row_to_product(r)
                if p["name"]:
                    all_products.append(p)

            logger.info(f"[API 수집] {end}번까지 완료, 누적 {len(all_products)}개")
            await asyncio.sleep(0.2)  # API 요청 제한 준수

    return all_products


def rank_and_save(products: list[dict]):
    """순위 매기고 products.json 저장."""
    # 카테고리별 순위
    cat_counts: dict[str, int] = {}
    sorted_all = sorted(products, key=lambda p: p["score"]["total"], reverse=True)

    for i, p in enumerate(sorted_all, 1):
        p["rank"] = i
        cat = p["category"]
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        p["category_rank"] = cat_counts[cat]

    output = {
        "generated_at": datetime.now().isoformat(),
        "total_count": len(sorted_all),
        "categories": sorted(list({p["category"] for p in sorted_all})),
        "products": sorted_all,
    }
    with open(PRODUCTS_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    logger.info(f"[완료] {PRODUCTS_JSON} 저장 ({len(sorted_all)}개)")
    cats = {}
    for p in sorted_all:
        cats[p["category"]] = cats.get(p["category"], 0) + 1
    for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
        logger.info(f"  {cat}: {cnt}개")


async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="식품안전나라 API 대량 수집기")
    parser.add_argument("--max", type=int, default=500, help="최대 수집 개수")
    parser.add_argument("--batch", type=int, default=100, help="배치 크기 (최대 1000)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    api_key = FOOD_SAFETY_API_KEY
    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("❌ FOOD_SAFETY_API_KEY가 설정되지 않았습니다.")
        print("   .env 파일에 FOOD_SAFETY_API_KEY=발급받은키 를 추가하세요.")
        return

    products = await collect_all(api_key, max_products=args.max, batch_size=args.batch)
    if products:
        rank_and_save(products)
        print(f"\n✅ 완료! {len(products)}개 제품 수집됨")
        print(f"   index.html을 브라우저로 열어 확인하세요.")
    else:
        print("❌ 수집된 데이터가 없습니다. API 키를 확인하세요.")


if __name__ == "__main__":
    asyncio.run(_main())
