"""
식품안전나라 OpenAPI 클라이언트
=================================
식품의약품안전처(MFDS) 공공데이터 API를 활용하여
공식 제품 정보, HACCP 인증, 행정처분 이력을 조회합니다.

API 키 발급: https://openapi.foodsafetykorea.go.kr
환경변수: FOOD_SAFETY_API_KEY

사용 가능한 주요 서비스:
  - C005: 가공식품 영양성분 DB
  - I2790: 식품 및 식품첨가물 공전
  - HACCP_CERTI_INFO: HACCP 인증 업체 조회
  - ADMIN_DISPO: 행정처분 이력 조회
  - C003: 알레르기 성분 DB
"""

import asyncio
import aiohttp
import logging
import os
import re
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

# 환경변수에서 API 키 로드
FOOD_SAFETY_API_KEY = os.environ.get("FOOD_SAFETY_API_KEY", "YOUR_API_KEY_HERE")
BASE_URL = "https://openapi.foodsafetykorea.go.kr/api"

# 식품안전나라 행정처분 정보 - 별도 REST API
ADMIN_DISPO_URL = "https://www.foodsafetykorea.go.kr/api/openApiServicesInfo.do"

# 공공데이터포털 HACCP 인증 API
HACCP_API_URL = "https://apis.data.go.kr/B553748/CertImgListServiceV3"
HACCP_API_KEY = os.environ.get("HACCP_API_KEY", "YOUR_HACCP_KEY_HERE")


class FoodSafetyAPIClient:
    """식품안전나라 OpenAPI 클라이언트."""

    def __init__(
        self,
        api_key: str = FOOD_SAFETY_API_KEY,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.api_key = api_key
        self._session = session
        self._owned_session = False

    async def __aenter__(self):
        if not self._session:
            self._session = aiohttp.ClientSession()
            self._owned_session = True
        return self

    async def __aexit__(self, *args):
        if self._owned_session and self._session:
            await self._session.close()

    # ── 공통 요청 메서드 ──────────────────────────
    async def _get_json(self, url: str, params: dict = None) -> dict:
        try:
            async with self._session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                else:
                    logger.warning(f"API 오류 {resp.status}: {url}")
                    return {}
        except Exception as e:
            logger.error(f"API 요청 실패: {e}")
            return {}

    async def _get_food_safety(
        self,
        service_name: str,
        start_idx: int = 1,
        end_idx: int = 20,
        extra_params: str = "",
    ) -> dict:
        """식품안전나라 표준 API 호출."""
        url = f"{BASE_URL}/{self.api_key}/{service_name}/json/{start_idx}/{end_idx}"
        if extra_params:
            url += f"/{extra_params}"
        return await self._get_json(url)

    # ── 가공식품 DB 조회 (C005) ────────────────────
    async def search_processed_food(
        self, product_name: str, manufacturer: str = ""
    ) -> list[dict]:
        """
        가공식품 영양성분 DB에서 제품 조회.
        Returns: 매칭 제품 목록
        """
        # C005 서비스: 가공식품 DB
        params = f"PRDLST_NM={quote(product_name)}"
        if manufacturer:
            params += f"/BSSH_NM={quote(manufacturer)}"

        data = await self._get_food_safety("C005", extra_params=params)
        rows = data.get("C005", {}).get("row", [])
        return rows

    # ── HACCP 인증 조회 ────────────────────────────
    async def get_haccp_certification(
        self, company_name: str
    ) -> Optional[dict]:
        """
        업체명으로 HACCP 인증 여부 조회.
        공공데이터포털 HACCP 인증 현황 API 사용.
        """
        params = {
            "serviceKey": HACCP_API_KEY,
            "pageNo": 1,
            "numOfRows": 10,
            "CMPNY_NM": company_name,
            "type": "json",
        }
        data = await self._get_json(f"{HACCP_API_URL}/getCertImgListV3", params=params)

        items = (
            data.get("body", {}).get("items", {}).get("item", [])
            if isinstance(data, dict)
            else []
        )
        if isinstance(items, dict):
            items = [items]

        if items:
            return {
                "certified": True,
                "company": items[0].get("CMPNY_NM"),
                "cert_no": items[0].get("HACCP_NO"),
                "valid_until": items[0].get("VALID_YMD"),
                "product": items[0].get("PRDT_NM"),
            }
        return {"certified": False}

    # ── 행정처분 이력 조회 ─────────────────────────
    async def get_admin_actions(
        self, company_name: str, years: int = 3
    ) -> list[dict]:
        """
        업체명으로 최근 N년 행정처분 이력 조회.
        식품안전나라 행정처분 공개 서비스 사용.
        """
        from datetime import datetime, timedelta
        date_from = (datetime.now() - timedelta(days=365 * years)).strftime("%Y%m%d")

        # 식품안전나라 행정처분 API (ADMIN_DISPO_INFO)
        params = f"BSSH_NM={quote(company_name)}/DSPS_YMD_FROM={date_from}"
        data = await self._get_food_safety("ADMIN_DISPO_INFO", end_idx=50, extra_params=params)

        rows = data.get("ADMIN_DISPO_INFO", {}).get("row", [])
        return [
            {
                "company": r.get("BSSH_NM"),
                "action_date": r.get("DSPS_YMD"),
                "action_type": r.get("DSPS_STLE_CD_NM"),
                "violation": r.get("VIOATN_ARTCL_NM"),
                "product": r.get("PRDLST_NM"),
            }
            for r in rows
        ]

    # ── 식품 바코드 조회 ───────────────────────────
    async def get_product_by_barcode(self, barcode: str) -> Optional[dict]:
        """
        바코드(BARCODE_NO)로 식품 정보 조회.
        C005 서비스 활용.
        """
        params = f"BARCODE_NO={barcode}"
        data = await self._get_food_safety("C005", extra_params=params)
        rows = data.get("C005", {}).get("row", [])
        if rows:
            r = rows[0]
            return {
                "name": r.get("PRDLST_NM"),
                "manufacturer": r.get("BSSH_NM"),
                "raw_ingredients": r.get("RAWMTRL_NM"),
                "capacity": r.get("CAPACITY"),
                "nutrition": {
                    "calories": r.get("NUTR_CONT1"),
                    "protein": r.get("NUTR_CONT4"),
                    "fat": r.get("NUTR_CONT5"),
                    "carbs": r.get("NUTR_CONT7"),
                    "sodium": r.get("NUTR_CONT8"),
                    "sugar": r.get("NUTR_CONT9"),
                },
                "certifications": self._parse_certifications_from_row(r),
            }
        return None

    def _parse_certifications_from_row(self, row: dict) -> list[str]:
        """식품안전나라 응답에서 인증 정보 파싱."""
        certs = []
        cert_fields = {
            "PRDLST_DCNM": {"유기농": "유기농", "무항생제": "무항생제", "HACCP": "HACCP"},
        }
        raw_text = " ".join(str(v) for v in row.values() if v)
        if "HACCP" in raw_text or "해썹" in raw_text:
            certs.append("HACCP")
        if "유기농" in raw_text:
            certs.append("유기농")
        if "무항생제" in raw_text:
            certs.append("무항생제")
        if "무농약" in raw_text:
            certs.append("무농약")
        return certs

    # ── 리콜 정보 조회 ─────────────────────────────
    async def get_recall_history(
        self, company_name: str = "", product_name: str = ""
    ) -> list[dict]:
        """식품 리콜 이력 조회 (RECALL_MANAGE 서비스)."""
        params_parts = []
        if company_name:
            params_parts.append(f"BSSH_NM={quote(company_name)}")
        if product_name:
            params_parts.append(f"PRDLST_NM={quote(product_name)}")

        params = "/".join(params_parts) if params_parts else ""
        data = await self._get_food_safety("RECALL_MANAGE", end_idx=20, extra_params=params)
        rows = data.get("RECALL_MANAGE", {}).get("row", [])
        return [
            {
                "company": r.get("BSSH_NM"),
                "product": r.get("PRDLST_NM"),
                "reason": r.get("RCLL_RESN"),
                "date": r.get("RCLL_YMD"),
                "status": r.get("RCLL_STLE_NM"),
            }
            for r in rows
        ]

    # ── 원재료 파싱 보조 ───────────────────────────
    def parse_raw_ingredients(self, raw_text: str) -> dict:
        """
        원재료명 문자열에서 구조화된 데이터 추출.
        예: "돼지고기(국내산)82%, 정제소금, 인산나트륨, 아질산나트륨"
        """
        if not raw_text:
            return {"main": None, "main_pct": None, "additives": []}

        # 주원료 및 함량
        main_match = re.search(
            r"^([가-힣a-zA-Z]+(?:\([^)]+\))?)\s*(\d+(?:\.\d+)?)%",
            raw_text.strip(),
        )
        main_name = main_match.group(1) if main_match else None
        main_pct = float(main_match.group(2)) if main_match else None

        # 첨가물 목록
        rest = raw_text[main_match.end():].strip().lstrip(",·/ ") if main_match else raw_text
        additive_tokens = [
            t.strip()
            for t in re.split(r"[,·/；;、\s]+", rest)
            if t.strip() and len(t.strip()) > 1
        ]

        return {
            "main": main_name,
            "main_pct": main_pct,
            "additives": additive_tokens,
            "additives_raw": rest,
        }


# ── 편의 함수 ────────────────────────────────────
async def enrich_product_with_official_data(
    product: dict,
    client: Optional[FoodSafetyAPIClient] = None,
) -> dict:
    """
    스크래핑한 제품 데이터를 식품안전나라 공식 데이터로 보강.
    - 바코드 있으면 정확한 원재료 정보 조회
    - 제조사 HACCP 인증 여부 확인
    - 행정처분/리콜 이력 조회
    """
    owned = client is None
    if owned:
        client = FoodSafetyAPIClient()
        await client.__aenter__()

    try:
        # 1. 바코드로 공식 데이터 조회
        barcode = product.get("barcode")
        if barcode:
            official = await client.get_product_by_barcode(barcode)
            if official:
                # 공식 원재료 데이터로 덮어쓰기 (더 신뢰성 높음)
                if official.get("raw_ingredients"):
                    product["raw_ingredient_text"] = official["raw_ingredients"]
                    parsed = client.parse_raw_ingredients(official["raw_ingredients"])
                    product["main_ingredient"] = parsed["main"]
                    product["main_ingredient_pct"] = parsed["main_pct"]
                    product["additives_raw"] = parsed["additives_raw"]
                product["official_nutrition"] = official.get("nutrition")
                # 인증 정보 보강
                for cert in official.get("certifications", []):
                    if cert not in product.get("certifications", []):
                        product.setdefault("certifications", []).append(cert)

        # 2. 제조사 HACCP 조회
        manufacturer = product.get("manufacturer") or product.get("brand", "")
        if manufacturer:
            haccp_info = await client.get_haccp_certification(manufacturer)
            if haccp_info.get("certified"):
                product["haccp"] = True
                product["haccp_info"] = haccp_info
                if "HACCP" not in product.get("certifications", []):
                    product.setdefault("certifications", []).append("HACCP")
            else:
                product.setdefault("haccp", False)

        # 3. 행정처분 이력 조회
        if manufacturer:
            actions = await client.get_admin_actions(manufacturer)
            product["admin_actions"] = len(actions)
            product["admin_action_detail"] = actions[:3]  # 최근 3건만 저장

        # 4. 리콜 이력 조회
        if manufacturer:
            recalls = await client.get_recall_history(
                company_name=manufacturer,
                product_name=product.get("name", ""),
            )
            product["recall_count"] = len(recalls)
            product["recall_detail"] = recalls[:3]

    finally:
        if owned:
            await client.__aexit__(None, None, None)

    return product


# ── CLI 테스트 ───────────────────────────────────
async def _main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--barcode", help="바코드 조회 테스트")
    parser.add_argument("--company", help="업체 HACCP 조회 테스트")
    parser.add_argument("--admin", help="행정처분 이력 조회 테스트")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    async with FoodSafetyAPIClient() as client:
        if args.barcode:
            result = await client.get_product_by_barcode(args.barcode)
            print(f"바코드 조회: {result}")
        if args.company:
            result = await client.get_haccp_certification(args.company)
            print(f"HACCP: {result}")
        if args.admin:
            result = await client.get_admin_actions(args.admin)
            print(f"행정처분: {result}")


if __name__ == "__main__":
    asyncio.run(_main())
