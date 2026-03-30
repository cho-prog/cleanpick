"""
쿠팡 식품 스크래퍼
==================
Playwright를 사용하여 쿠팡 카테고리 페이지에서 식품 목록을 수집하고
각 상품의 상세정보(원재료, 인증, 바코드 등)를 파싱합니다.

사용법:
    python -m scraper.coupang_scraper --category 가공육 --max 50
"""

import asyncio
import json
import re
import time
import random
import logging
from typing import Optional
from dataclasses import dataclass, asdict

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 카테고리 ID 매핑 (쿠팡 카테고리 코드)
# ──────────────────────────────────────────────
COUPANG_CATEGORY_MAP = {
    "가공육":       {"url": "https://www.coupang.com/np/categories/1280034", "name": "햄·소시지·베이컨"},
    "음료":         {"url": "https://www.coupang.com/np/categories/1280047", "name": "주스·음료"},
    "유제품":       {"url": "https://www.coupang.com/np/categories/1280050", "name": "우유·요거트·치즈"},
    "과자":         {"url": "https://www.coupang.com/np/categories/1280055", "name": "과자·스낵"},
    "면류":         {"url": "https://www.coupang.com/np/categories/1280030", "name": "라면·파스타·국수"},
    "통조림":       {"url": "https://www.coupang.com/np/categories/1280032", "name": "통조림·캔"},
    "냉동식품":     {"url": "https://www.coupang.com/np/categories/1280025", "name": "냉동식품"},
    "소스·조미료":  {"url": "https://www.coupang.com/np/categories/1280045", "name": "소스·드레싱·양념"},
    "빵·베이커리":  {"url": "https://www.coupang.com/np/categories/1280060", "name": "빵·케이크·떡"},
    "두부·콩나물":  {"url": "https://www.coupang.com/np/categories/1280015", "name": "두부·콩나물·숙주"},
    "김치·반찬":    {"url": "https://www.coupang.com/np/categories/1280020", "name": "김치·장아찌·반찬"},
}


@dataclass
class CoupangProduct:
    product_id: str          # 쿠팡 상품 ID
    name: str
    brand: str
    category: str
    price: Optional[int]
    image_url: str
    coupang_url: str         # 파트너스 링크 (runner에서 변환)
    barcode: Optional[str]
    manufacturer: Optional[str]
    origin: Optional[str]    # 원산지
    weight: Optional[str]    # 용량/중량
    # 원재료 관련
    raw_ingredient_text: str  # 원재료명 전체 원문
    main_ingredient: Optional[str]
    main_ingredient_pct: Optional[float]
    additives_raw: str        # 첨가물 원문
    # 인증
    certifications: list      # ['HACCP', '유기농', ...]
    # 스크래핑 메타
    scraped_at: str
    source: str = "coupang"


class CoupangScraper:
    """쿠팡 상품 스크래퍼."""

    BASE_URL = "https://www.coupang.com"
    SEARCH_URL = "https://www.coupang.com/np/search?q={query}&channel=user&isTabSearch=N"

    # 원재료명 섹션 키워드
    INGREDIENT_KEYS = ["원재료명", "원재료", "성분", "원료", "재료"]
    ADDITIVE_SPLIT_PATTERN = re.compile(
        r"[,·/；;、]|(?:\s+(?:및|과|와)\s+)"
    )

    def __init__(self, headless: bool = True, delay_range: tuple = (1.5, 3.5)):
        self.headless = headless
        self.delay_range = delay_range
        self._browser = None
        self._context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        # 봇 감지 회피: navigator.webdriver 숨기기
        await self._context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
        """)
        return self

    async def __aexit__(self, *args):
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    async def _delay(self):
        await asyncio.sleep(random.uniform(*self.delay_range))

    async def _new_page(self) -> Page:
        page = await self._context.new_page()
        page.set_default_timeout(30_000)
        return page

    # ── 카테고리 목록 수집 ────────────────────────
    async def get_product_ids_from_category(
        self, category_key: str, max_pages: int = 5
    ) -> list[str]:
        """카테고리 페이지에서 상품 ID 목록 수집."""
        cat_info = COUPANG_CATEGORY_MAP.get(category_key)
        if not cat_info:
            logger.warning(f"미등록 카테고리: {category_key}")
            return []

        ids = []
        page = await self._new_page()
        try:
            for page_no in range(1, max_pages + 1):
                url = f"{cat_info['url']}?page={page_no}&per_page=72&filterType=rocket"
                await page.goto(url, wait_until="domcontentloaded")
                await self._delay()

                # 상품 링크 추출
                anchors = await page.query_selector_all(
                    "a[href*='/vp/products/']"
                )
                for a in anchors:
                    href = await a.get_attribute("href")
                    if href:
                        m = re.search(r"/vp/products/(\d+)", href)
                        if m and m.group(1) not in ids:
                            ids.append(m.group(1))

                logger.info(f"[쿠팡] {category_key} 페이지 {page_no}: {len(ids)}개 누적")

                # 다음 페이지 없으면 종료
                next_btn = await page.query_selector(".next.page-item:not(.disabled)")
                if not next_btn:
                    break

        except Exception as e:
            logger.error(f"카테고리 수집 오류: {e}")
        finally:
            await page.close()

        return ids

    # ── 검색으로 상품 ID 수집 ─────────────────────
    async def search_product_ids(self, query: str, max_results: int = 50) -> list[str]:
        """검색어로 상품 ID 목록 수집."""
        ids = []
        page = await self._new_page()
        try:
            url = self.SEARCH_URL.format(query=query)
            await page.goto(url, wait_until="domcontentloaded")
            await self._delay()

            # 스크롤로 지연 로딩 항목 로드
            for _ in range(3):
                await page.evaluate("window.scrollBy(0, 1200)")
                await asyncio.sleep(0.8)

            anchors = await page.query_selector_all("a[href*='/vp/products/']")
            for a in anchors:
                href = await a.get_attribute("href")
                if href:
                    m = re.search(r"/vp/products/(\d+)", href)
                    if m and m.group(1) not in ids:
                        ids.append(m.group(1))
                if len(ids) >= max_results:
                    break
        finally:
            await page.close()

        return ids

    # ── 상품 상세 페이지 파싱 ─────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=8))
    async def get_product_detail(
        self, product_id: str, category: str = "기타"
    ) -> Optional[CoupangProduct]:
        """상품 상세 페이지에서 정보 파싱."""
        url = f"{self.BASE_URL}/vp/products/{product_id}"
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="networkidle")
            await self._delay()

            # ─ 상품명
            name = await self._get_text(page, [
                ".prod-buy-header__title",
                "h2.prod-buy-header__title",
                "#itemTitle",
            ])

            # ─ 브랜드
            brand = await self._get_text(page, [
                ".prod-brand-name",
                ".vendor-item__name",
            ])

            # ─ 가격
            price_raw = await self._get_text(page, [
                ".prod-price-main",
                ".prod-price .total-price strong",
            ])
            price = self._parse_price(price_raw)

            # ─ 이미지
            img_el = await page.query_selector(".prod-image__item img, #repImageContainer img")
            image_url = await img_el.get_attribute("src") if img_el else ""

            # ─ 상품 상세 정보 테이블 (원재료, 제조사, 바코드 등)
            detail_info = await self._parse_detail_table(page)

            # ─ 원재료명 파싱
            raw_text = detail_info.get("원재료명", detail_info.get("성분", ""))
            main_ingr, main_pct, additives_raw = self._parse_ingredient_text(raw_text)

            # ─ 인증 파싱 (상품 상세 이미지 alt/텍스트 기반 + 테이블)
            certs = self._extract_certifications(detail_info, name or "")

            import datetime
            return CoupangProduct(
                product_id=product_id,
                name=name or "",
                brand=brand or detail_info.get("제조원", detail_info.get("브랜드", "")),
                category=category,
                price=price,
                image_url=image_url or "",
                coupang_url=url,
                barcode=detail_info.get("바코드", detail_info.get("상품바코드")),
                manufacturer=detail_info.get("제조원", detail_info.get("수입원")),
                origin=detail_info.get("원산지", detail_info.get("제조국")),
                weight=detail_info.get("중량", detail_info.get("용량", detail_info.get("내용량"))),
                raw_ingredient_text=raw_text,
                main_ingredient=main_ingr,
                main_ingredient_pct=main_pct,
                additives_raw=additives_raw,
                certifications=certs,
                scraped_at=datetime.datetime.now().isoformat(),
            )

        except PWTimeout:
            logger.warning(f"타임아웃: product_id={product_id}")
            return None
        except Exception as e:
            logger.error(f"상세 파싱 오류 {product_id}: {e}")
            raise
        finally:
            await page.close()

    async def _get_text(self, page: Page, selectors: list[str]) -> str:
        """여러 셀렉터 중 첫 번째 매칭 텍스트 반환."""
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    text = await el.inner_text()
                    if text.strip():
                        return text.strip()
            except Exception:
                continue
        return ""

    async def _parse_detail_table(self, page: Page) -> dict:
        """
        상품 상세 정보 테이블 파싱.
        쿠팡은 .prod-attr-table, .spec-list 등 다양한 구조 사용.
        """
        result = {}

        # 방법 1: dl/dt/dd 구조
        try:
            dts = await page.query_selector_all(".prod-attr-table dt, .spec-list dt")
            dds = await page.query_selector_all(".prod-attr-table dd, .spec-list dd")
            for dt, dd in zip(dts, dds):
                key = (await dt.inner_text()).strip().rstrip(":")
                val = (await dd.inner_text()).strip()
                if key and val:
                    result[key] = val
        except Exception:
            pass

        # 방법 2: table tr 구조
        try:
            rows = await page.query_selector_all("table.prod-attr-table tr, table.spec-table tr")
            for row in rows:
                tds = await row.query_selector_all("th, td")
                if len(tds) >= 2:
                    key = (await tds[0].inner_text()).strip().rstrip(":")
                    val = (await tds[1].inner_text()).strip()
                    if key and val:
                        result[key] = val
        except Exception:
            pass

        # 방법 3: 상품 상세 설명 영역 텍스트 파싱 (원재료명 포함 경우)
        if not any(k in result for k in self.INGREDIENT_KEYS):
            try:
                desc_text = await page.inner_text(".prod-description, #productDescription, .item-detail-desc")
                ingr_match = re.search(
                    r"원재료(?:명)?\s*[:\s]*([^\n]{10,500})",
                    desc_text,
                    re.IGNORECASE,
                )
                if ingr_match:
                    result["원재료명"] = ingr_match.group(1).strip()
            except Exception:
                pass

        return result

    def _parse_price(self, raw: str) -> Optional[int]:
        """가격 문자열 → 정수 변환."""
        if not raw:
            return None
        digits = re.sub(r"[^\d]", "", raw)
        return int(digits) if digits else None

    def _parse_ingredient_text(self, raw: str) -> tuple[Optional[str], Optional[float], str]:
        """
        원재료명 원문에서 주원료·함량·첨가물 추출.
        예: "돼지고기(국내산)82%, 소금, 인산나트륨, 아질산나트륨"
        Returns: (주원료명, 함량%, 첨가물_원문)
        """
        if not raw:
            return None, None, ""

        # 주원료 및 함량 추출
        # 패턴: [원료명](산지?) [숫자]%
        main_pattern = re.search(
            r"^([가-힣a-zA-Z\s\(\)]+?)\s*(?:\([^)]*\))?\s*(\d+(?:\.\d+)?)\s*%",
            raw.strip(),
        )
        main_name = None
        main_pct = None
        if main_pattern:
            main_name = main_pattern.group(1).strip().rstrip(",·")
            main_pct = float(main_pattern.group(2))

        # 첨가물 추출: 주원료 이후의 성분들
        # 식품첨가물은 보통 화학명으로 표기됨
        # 원문에서 주원료 이후 나열된 성분들을 추출
        after_main = raw
        if main_pattern:
            after_main = raw[main_pattern.end():].strip().lstrip(",·/ ")

        # 위험 첨가물 키워드 필터링
        additive_keywords = [
            "나트륨", "칼슘", "칼륨", "인산", "MSG", "글루탐산",
            "색소", "착색", "타르", "보존", "향료", "감미", "아질산",
            "소르빈", "안식향", "BHA", "BHT", "카라기난", "레시틴",
            "유화제", "증점제", "산도조절", "팽창제", "산화방지",
        ]

        # 간단히: 주원료 이후 전체를 첨가물로 처리
        additives_raw = after_main if after_main else ""

        return main_name, main_pct, additives_raw

    def _extract_certifications(self, detail_info: dict, name: str) -> list[str]:
        """상세 정보와 상품명에서 인증 정보 추출."""
        certs = []
        combined_text = " ".join(detail_info.values()) + " " + name

        cert_keywords = {
            "HACCP": ["HACCP", "해썹"],
            "유기농": ["유기농", "유기인증", "organic"],
            "무항생제": ["무항생제", "antibiotic free"],
            "무농약": ["무농약"],
            "동물복지": ["동물복지"],
            "비GMO": ["비GMO", "Non-GMO", "non gmo"],
            "ISO22000": ["ISO22000", "ISO 22000"],
            "친환경": ["친환경인증", "친환경 인증"],
            "GAP": ["GAP인증", "GAP 인증"],
        }

        for cert_name, keywords in cert_keywords.items():
            for kw in keywords:
                if kw.lower() in combined_text.lower():
                    if cert_name not in certs:
                        certs.append(cert_name)
                    break

        return certs

    # ── 배치 수집 ──────────────────────────────────
    async def scrape_category(
        self,
        category: str,
        max_pages: int = 3,
        max_products: int = 100,
    ) -> list[dict]:
        """카테고리 전체 스크래핑 후 딕셔너리 목록 반환."""
        logger.info(f"[쿠팡] 카테고리 '{category}' 스크래핑 시작")

        ids = await self.get_product_ids_from_category(category, max_pages=max_pages)
        ids = ids[:max_products]
        logger.info(f"[쿠팡] 상품 ID {len(ids)}개 수집")

        results = []
        for i, pid in enumerate(ids, 1):
            try:
                product = await self.get_product_detail(pid, category=category)
                if product:
                    results.append(asdict(product))
                    logger.info(f"[쿠팡] {i}/{len(ids)} {product.name[:30]}")
            except Exception as e:
                logger.warning(f"[쿠팡] {pid} 실패: {e}")
            await self._delay()

        logger.info(f"[쿠팡] 완료: {len(results)}/{len(ids)}개")
        return results


# ── CLI 실행 ────────────────────────────────────
async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="쿠팡 식품 스크래퍼")
    parser.add_argument("--category", default="가공육")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-products", type=int, default=50)
    parser.add_argument("--output", default="data/coupang_raw.json")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async with CoupangScraper(headless=not args.no_headless) as scraper:
        products = await scraper.scrape_category(
            args.category,
            max_pages=args.max_pages,
            max_products=args.max_products,
        )

    out_path = os.path.join(os.path.dirname(__file__), "..", args.output)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    print(f"저장 완료: {out_path} ({len(products)}개)")


if __name__ == "__main__":
    import os
    asyncio.run(_main())
