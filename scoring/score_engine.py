"""
PurePick 종합 점수 산출 엔진
=============================
점수 구성 (100점 만점):
  1. 주원료 함량  (30점) - 핵심 원재료가 차지하는 비율
  2. 식품첨가물   (35점) - 첨가물 종류·수·위험도
  3. 인증·등급    (20점) - 유기농, 무항생제, HACCP, ISO 등
  4. 위생/행정    (15점) - 행정처분이력, 리콜이력

패널티 방식:
  각 영역 만점에서 감점 → 최종 합산
"""

import json
import re
import os
from typing import Optional

# 첨가물 안전성 DB 로드
_DB_PATH = os.path.join(os.path.dirname(__file__), "../data/additives_safety.json")
try:
    with open(_DB_PATH, encoding="utf-8") as f:
        _ADDITIVE_DB: dict = json.load(f)["additives"]
except Exception:
    _ADDITIVE_DB = {}

# ──────────────────────────────────────────────
# 1. 주원료 함량 점수 (30점)
# ──────────────────────────────────────────────
def score_main_ingredient(main_ingredient_pct: Optional[float], category: str = "") -> dict:
    """
    주원료(핵심 원재료) 함량 비율로 점수 산출.
    - 100%: 30점 (착즙음료, 순수원유 등)
    - 80% 이상: 27~30점
    - 50% 미만: 급격히 감점
    - 불명확: 카테고리 평균값 사용

    Returns:
        {"score": int, "max": 30, "detail": str}
    """
    MAX = 30

    # 카테고리별 기본 함량 추정값 (데이터 없을 때)
    CATEGORY_DEFAULTS = {
        "가공육": 55.0,
        "음료": 30.0,
        "유제품": 80.0,
        "과자": 35.0,
        "면류": 40.0,
        "조미료": 20.0,
        "통조림": 45.0,
        "냉동식품": 40.0,
        "빵류": 35.0,
        "기타": 40.0,
    }

    if main_ingredient_pct is None:
        pct = CATEGORY_DEFAULTS.get(category, 40.0)
        source = f"카테고리 평균 추정({pct:.0f}%)"
    else:
        pct = float(main_ingredient_pct)
        source = f"{pct:.1f}%"

    # 비선형 점수 곡선
    if pct >= 100:
        score = MAX
    elif pct >= 85:
        score = int(MAX * (0.93 + (pct - 85) / 100 * 0.07))
    elif pct >= 70:
        score = int(MAX * (0.80 + (pct - 70) / 15 * 0.13))
    elif pct >= 55:
        score = int(MAX * (0.62 + (pct - 55) / 15 * 0.18))
    elif pct >= 40:
        score = int(MAX * (0.42 + (pct - 40) / 15 * 0.20))
    elif pct >= 25:
        score = int(MAX * (0.20 + (pct - 25) / 15 * 0.22))
    else:
        score = max(0, int(MAX * pct / 25 * 0.20))

    return {"score": min(MAX, score), "max": MAX, "detail": source}


# ──────────────────────────────────────────────
# 2. 식품첨가물 점수 (35점)
# ──────────────────────────────────────────────
def _parse_additives(additives_raw: str) -> list[str]:
    """원재료명 문자열에서 첨가물 토큰 추출."""
    if not additives_raw:
        return []
    # 구분자: ·, ,, /, ;, 공백
    tokens = re.split(r"[·,/;\s]+", additives_raw.strip())
    return [t.strip() for t in tokens if t.strip()]


def _lookup_additive(name: str) -> Optional[dict]:
    """첨가물명으로 DB 조회 (부분 매칭 포함)."""
    name = name.strip()
    # 정확 매칭
    if name in _ADDITIVE_DB:
        return _ADDITIVE_DB[name]
    # 부분 매칭 (DB 키가 입력에 포함되거나 반대)
    for key, val in _ADDITIVE_DB.items():
        if key in name or name in key:
            return val
    return None


def score_additives(additives_raw: str) -> dict:
    """
    첨가물 문자열을 파싱하여 안전성 점수 산출.

    Returns:
        {"score": int, "max": 35, "items": [...], "total_penalty": float}
    """
    MAX = 35

    if not additives_raw or additives_raw.strip() in ("", "없음", "첨가물 없음", "-"):
        return {
            "score": MAX,
            "max": MAX,
            "items": [],
            "total_penalty": 0,
            "detail": "첨가물 없음",
        }

    tokens = _parse_additives(additives_raw)
    items = []
    total_penalty = 0.0

    for token in tokens:
        info = _lookup_additive(token)
        if info:
            penalty = info["penalty"]
            items.append({
                "name": token,
                "en": info.get("en", ""),
                "category": info.get("category", ""),
                "penalty": penalty,
                "concern": info.get("concern", ""),
            })
        else:
            # 미등록 첨가물: 중간 패널티
            penalty = 2
            items.append({
                "name": token,
                "en": "",
                "category": "unknown",
                "penalty": penalty,
                "concern": "DB 미등록 첨가물",
            })
        total_penalty += penalty

    # 첨가물 수 자체에도 패널티 (종류가 많을수록 가중)
    count_penalty = len(tokens) * 0.5  # 항목당 0.5점 추가 패널티
    total_penalty += count_penalty

    # 최대 페널티 한도: 35점 전체를 다 깎을 수 있음
    raw_score = MAX - total_penalty
    score = max(0, min(MAX, int(raw_score)))

    return {
        "score": score,
        "max": MAX,
        "items": items,
        "total_penalty": round(total_penalty, 1),
        "detail": f"{len(tokens)}종 첨가물",
    }


# ──────────────────────────────────────────────
# 3. 인증·등급 점수 (20점)
# ──────────────────────────────────────────────
CERT_SCORES = {
    "유기농": 12,          # 최고 인증 (원재료 전반 기준)
    "organic": 12,
    "무농약": 9,
    "무항생제": 8,
    "antibiotic_free": 8,
    "non_gmo": 5,          # 비GMO
    "non-gmo": 5,
    "비GMO": 5,
    "동물복지": 6,
    "animal_welfare": 6,
    "HACCP": 7,            # 위생관리 인증
    "haccp": 7,
    "ISO22000": 6,
    "iso22000": 6,
    "ISO9001": 3,
    "iso9001": 3,
    "GAP": 4,              # 우수농산물관리제도
    "gap": 4,
    "친환경": 7,
    "녹색인증": 4,
    "KS": 2,
}


def score_certifications(certifications: list[str]) -> dict:
    """
    인증 목록으로 점수 산출 (중복 인증은 합산, 최대 20점).

    Returns:
        {"score": int, "max": 20, "detail": str}
    """
    MAX = 20
    if not certifications:
        return {"score": 0, "max": MAX, "detail": "인증 없음"}

    total = 0
    matched = []
    for cert in certifications:
        key = cert.strip()
        pts = CERT_SCORES.get(key, CERT_SCORES.get(key.upper(), 0))
        if pts:
            total += pts
            matched.append(f"{key}(+{pts})")

    score = min(MAX, total)
    detail = ", ".join(matched) if matched else "미인정 인증"
    return {"score": score, "max": MAX, "detail": detail}


# ──────────────────────────────────────────────
# 4. 위생·행정처분 점수 (15점)
# ──────────────────────────────────────────────
def score_hygiene(
    haccp: bool = False,
    iso_food: bool = False,
    admin_actions: int = 0,     # 최근 3년 행정처분 건수
    recall_count: int = 0,      # 최근 3년 리콜 건수
) -> dict:
    """
    위생관리 및 행정처분 이력 점수.

    Returns:
        {"score": int, "max": 15, "detail": str}
    """
    MAX = 15
    score = 5  # 기본 점수

    if haccp:
        score += 6
    if iso_food:  # ISO 22000 / SQF 등 식품안전 국제표준
        score += 4

    # 행정처분 패널티
    action_penalty = min(10, admin_actions * 3)
    # 리콜 패널티
    recall_penalty = min(8, recall_count * 4)

    score = max(0, score - action_penalty - recall_penalty)
    score = min(MAX, score)

    parts = []
    if haccp:
        parts.append("HACCP")
    if iso_food:
        parts.append("ISO식품안전")
    if admin_actions:
        parts.append(f"행정처분 {admin_actions}건")
    if recall_count:
        parts.append(f"리콜 {recall_count}건")
    if not parts:
        parts.append("기본")

    return {"score": score, "max": MAX, "detail": " | ".join(parts)}


# ──────────────────────────────────────────────
# 종합 점수 산출
# ──────────────────────────────────────────────
def compute_total_score(product: dict) -> dict:
    """
    제품 딕셔너리를 받아 종합 점수 계산.

    product 필드:
        name: str
        category: str
        main_ingredient_pct: float | None  (주원료 함량 %)
        additives: str                     (첨가물 원문)
        certifications: list[str]          (인증 목록)
        haccp: bool
        iso_food: bool
        admin_actions: int
        recall_count: int

    Returns:
        {
            "total": int,         # 100점 만점 종합
            "grade": str,         # 우수/양호/보통/주의
            "grade_color": str,   # CSS 클래스
            "breakdown": {
                "ingredient": {...},
                "additive": {...},
                "certification": {...},
                "hygiene": {...},
            }
        }
    """
    s_ingr = score_main_ingredient(
        product.get("main_ingredient_pct"),
        product.get("category", "기타"),
    )
    s_add = score_additives(product.get("additives", ""))
    s_cert = score_certifications(product.get("certifications", []))
    s_hyg = score_hygiene(
        haccp=product.get("haccp", False),
        iso_food=product.get("iso_food", False),
        admin_actions=product.get("admin_actions", 0),
        recall_count=product.get("recall_count", 0),
    )

    total = s_ingr["score"] + s_add["score"] + s_cert["score"] + s_hyg["score"]
    total = max(0, min(100, total))

    if total >= 85:
        grade, grade_color = "우수", "grade-excellent"
    elif total >= 70:
        grade, grade_color = "양호", "grade-good"
    elif total >= 50:
        grade, grade_color = "보통", "grade-average"
    else:
        grade, grade_color = "주의", "grade-caution"

    return {
        "total": total,
        "grade": grade,
        "grade_color": grade_color,
        "breakdown": {
            "ingredient": s_ingr,
            "additive": s_add,
            "certification": s_cert,
            "hygiene": s_hyg,
        },
    }


# ──────────────────────────────────────────────
# 카테고리별 상대 순위 산출
# ──────────────────────────────────────────────
def rank_products(products: list[dict]) -> list[dict]:
    """
    products: compute_total_score 결과가 포함된 product 딕셔너리 목록
    각 제품에 rank(전체), category_rank 추가하여 반환.
    """
    # 전체 순위
    sorted_all = sorted(products, key=lambda p: p.get("score", {}).get("total", 0), reverse=True)
    for i, p in enumerate(sorted_all, 1):
        p["rank"] = i

    # 카테고리별 순위
    from collections import defaultdict
    by_cat: dict[str, list] = defaultdict(list)
    for p in products:
        by_cat[p.get("category", "기타")].append(p)

    for cat_products in by_cat.values():
        sorted_cat = sorted(cat_products, key=lambda p: p.get("score", {}).get("total", 0), reverse=True)
        for i, p in enumerate(sorted_cat, 1):
            p["category_rank"] = i

    return products
