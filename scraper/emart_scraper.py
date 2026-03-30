"""
이마트몰(SSG.com) 식품 스크래퍼
=================================
SSG.com의 이마트 식품 카테고리에서 상품을 수집합니다.
Playwright 기반으로 JS 렌더링 처리.

사용법:
    python -m scraper.emart_scraper --category 가공육 --max 50
"""

import asyncio
import json
import re
import random
import logging
import os
from typing import Optional
from dataclasses import dataclass, asdict

from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# 이마트몰(SSG) 카테고리 URL 매핑
# ──────────────────────────────────────────────
EMART_CATEGORY_MAP = {
    "가공육": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079754&tgt=EMAUL",
        "name": "햄·소시지",
    },
    "음료": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079748&tgt=EMAUL",
        "name": "음료",
    },
    "유제품": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079745&tgt=EMAUL",
        "name": "우유·유제품",
    },
    "과자": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079752&tgt=EMAUL",
        "name": "과자·스낵",
    },
    "면류": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079756&tgt=EMAUL",
        "name": "라면·면류",
    },
    "통조림": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079760&tgt=EMAUL",
        "name": "통조림·캔",
    },
    "냉동식품": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079731&tgt=EMAUL",
        "name": "냉동식품",
    },
    "소스·조미료": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079762&tgt=EMAUL",
        "name": "소스·양념",
    },
    "빵·베이커리": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079750&tgt=EMAUL",
        "name": "빵·케이크",
    },
    "두부·콩나물": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079741&tgt=EMAUL",
        "name": "두부·콩나물",
    },
    "김치·반찬": {
        "url": "https://www.ssg.com/category/categoryList.ssg?ctgId=6000079742&tgt=EMAUL",
        "name": "김치·반찬",
    },
}


@dataclass
class EmartProduct:
    product_id: str          # SSG 상품 ID
    name: str
    brand: str
    category: str
    price: Optional[int]
    image_url: str
    ssg_url: str
    barcode: Optional[str]
    manufacturer: Optional[str]
    origin: Optional[str]
    weight: Optional[str]
    raw_ingredient_text: str
    main_ingredient: Optional[str]
    main_ingredient_pct: Optional[float]
    additives_raw: str
    certifications: list
    scraped_at: str
    source: str = "emart"


class EmartScraper:
    """이마트몰(SSG.com) 상품 스크래퍼."""

    BASE_URL = "https://www.ssg.com"
    SEARCH_URL = "https://www.ssg.com/search.ssg?target=emaul&query={query}"

    def __init__(self, headless: bool = True, delay_range: tuple = (2.0, 4.0)):
        self.headless = headless
        self.delay_range = delay_range
        self._pw = None
        self._browser = None
        self._context = None

    async def __aenter__(self):
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        self._context = await self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="ko-KR",
            timezone_id="Asia/Seoul",
        )
        await self._context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
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

    # ── 카테고리 상품 ID 수집 ─────────────────────
    async def get_product_ids_from_category(
        self, category_key: str, max_pages: int = 5
    ) -> list[str]:
        cat_info = EMART_CATEGORY_MAP.get(category_key)
        if not cat_info:
            logger.warning(f"미등록 카테고리: {category_key}")
            return []

        ids = []
        page = await self._new_page()
        try:
            for page_no in range(1, max_pages + 1):
                url = f"{cat_info['url']}&pageNo={page_no}&sort=sales"
                await page.goto(url, wait_until="domcontentloaded")
                await self._delay()

                # 더보기 버튼 클릭 (있을 경우)
                try:
                    more_btn = await page.query_selector(".btn_more_item, .load_more")
                    if more_btn:
                        await more_btn.click()
                        await asyncio.sleep(1.5)
                except Exception:
                    pass

                # 상품 링크 추출
                # SSG URL 패턴: /item/itemView.ssg?itemId=xxxxxx
                anchors = await page.query_selector_all("a[href*='itemView.ssg']")
                for a in anchors:
                    href = await a.get_attribute("href")
                    if href:
                        m = re.search(r"itemId=(\d+)", href)
                        if m and m.group(1) not in ids:
                            ids.append(m.group(1))

                logger.info(f"[이마트] {category_key} 페이지 {page_no}: {len(ids)}개 누적")

                # 마지막 페이지 감지
                last_page_el = await page.query_selector(".pagination .on:last-child, .paging .active:last-child")
                if last_page_el or page_no >= max_pages:
                    break

        except Exception as e:
            logger.error(f"카테고리 수집 오류: {e}")
        finally:
            await page.close()

        return ids

    # ── 상품 상세 파싱 ─────────────────────────────
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=8))
    async def get_product_detail(
        self, product_id: str, category: str = "기타"
    ) -> Optional[EmartProduct]:
        url = f"{self.BASE_URL}/item/itemView.ssg?itemId={product_id}"
        page = await self._new_page()
        try:
            await page.goto(url, wait_until="networkidle")
            await self._delay()

            # ─ 상품명
            name = await self._get_text(page, [
                ".cdtl_info_tit",
                "h2.item_tit",
                ".item_name",
            ])

            # ─ 브랜드/제조사
            brand = await self._get_text(page, [
                ".brand_name",
                ".cdtl_brand",
            ])

            # ─ 가격
            price_raw = await self._get_text(page, [
                ".cdtl_price .price_cpr strong",
                ".price_real",
                ".ssg_price",
            ])
            price = self._parse_price(price_raw)

            # ─ 이미지
            img_el = await page.query_selector(".cdtl_img_main img, .item_img img")
            image_url = await img_el.get_attribute("src") if img_el else ""

            # ─ 상세 정보 테이블
            detail_info = await self._parse_detail_table(page)

            # ─ 원재료 파싱
            raw_text = detail_info.get("원재료명", detail_info.get("원재료", ""))
            main_ingr, main_pct, additives_raw = self._parse_ingredient_text(raw_text)

            # ─ 인증 추출
            certs = self._extract_certifications(detail_info, name or "")

            import datetime
            return EmartProduct(
                product_id=product_id,
                name=name or "",
                brand=brand or detail_info.get("제조원", ""),
                category=category,
                price=price,
                image_url=image_url or "",
                ssg_url=url,
                barcode=detail_info.get("바코드"),
                manufacturer=detail_info.get("제조원", detail_info.get("수입원")),
                origin=detail_info.get("원산지"),
                weight=detail_info.get("중량", detail_info.get("내용량")),
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
        """SSG 상품 상세 정보 테이블 파싱."""
        result = {}

        # 방법 1: dl 구조 (.cdtl_info_lst)
        try:
            dts = await page.query_selector_all(".cdtl_info_lst dt, .item_spec_list dt")
            dds = await page.query_selector_all(".cdtl_info_lst dd, .item_spec_list dd")
            for dt, dd in zip(dts, dds):
                key = (await dt.inner_text()).strip().rstrip(":")
                val = (await dd.inner_text()).strip()
                if key and val:
                    result[key] = val
        except Exception:
            pass

        # 방법 2: table 구조
        try:
            rows = await page.query_selector_all("table.cdtl_tbl tr, table.item_spec tr")
            for row in rows:
                cells = await row.query_selector_all("th, td")
                if len(cells) >= 2:
                    key = (await cells[0].inner_text()).strip().rstrip(":")
                    val = (await cells[1].inner_text()).strip()
                    if key and val:
                        result[key] = val
        except Exception:
            pass

        # 방법 3: 상세 설명 이미지 alt에서 텍스트 추출 (이미지 기반 원재료 페이지)
        # → OCR이 필요하므로 기본 스킵, food_safety_api로 보완

        return result

    def _parse_price(self, raw: str) -> Optional[int]:
        if not raw:
            return None
        digits = re.sub(r"[^\d]", "", raw)
        return int(digits) if digits else None

    def _parse_ingredient_text(self, raw: str) -> tuple[Optional[str], Optional[float], str]:
        if not raw:
            return None, None, ""

        main_pattern = re.search(
            r"^([가-힣a-zA-Z\s\(\)]+?)\s*(?:\([^)]*\))?\s*(\d+(?:\.\d+)?)\s*%",
            raw.strip(),
        )
        main_name = None
        main_pct = None
        if main_pattern:
            main_name = main_pattern.group(1).strip().rstrip(",·")
            main_pct = float(main_pattern.group(2))

        after_main = raw
        if main_pattern:
            after_main = raw[main_pattern.end():].strip().lstrip(",·/ ")

        return main_name, main_pct, after_main

    def _extract_certifications(self, detail_info: dict, name: str) -> list[str]:
        certs = []
        combined = " ".join(detail_info.values()) + " " + name

        cert_keywords = {
            "HACCP": ["HACCP", "해썹"],
            "유기농": ["유기농", "유기인증"],
            "무항생제": ["무항생제"],
            "무농약": ["무농약"],
            "동물복지": ["동물복지"],
            "비GMO": ["비GMO", "Non-GMO"],
            "ISO22000": ["ISO22000", "ISO 22000"],
            "친환경": ["친환경인증"],
            "GAP": ["GAP인증"],
        }

        for cert_name, keywords in cert_keywords.items():
            for kw in keywords:
                if kw.lower() in combined.lower():
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
        logger.info(f"[이마트] 카테고리 '{category}' 스크래핑 시작")

        ids = await self.get_product_ids_from_category(category, max_pages=max_pages)
        ids = ids[:max_products]
        logger.info(f"[이마트] 상품 ID {len(ids)}개 수집")

        results = []
        for i, pid in enumerate(ids, 1):
            try:
                product = await self.get_product_detail(pid, category=category)
                if product:
                    results.append(asdict(product))
                    logger.info(f"[이마트] {i}/{len(ids)} {product.name[:30]}")
            except Exception as e:
                logger.warning(f"[이마트] {pid} 실패: {e}")
            await self._delay()

        logger.info(f"[이마트] 완료: {len(results)}/{len(ids)}개")
        return results


# ── CLI 실행 ────────────────────────────────────
async def _main():
    import argparse
    parser = argparse.ArgumentParser(description="이마트몰 식품 스크래퍼")
    parser.add_argument("--category", default="가공육")
    parser.add_argument("--max-pages", type=int, default=3)
    parser.add_argument("--max-products", type=int, default=50)
    parser.add_argument("--output", default="data/emart_raw.json")
    parser.add_argument("--no-headless", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    async with EmartScraper(headless=not args.no_headless) as scraper:
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
    asyncio.run(_main())
