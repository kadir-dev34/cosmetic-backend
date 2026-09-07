import os
import re
import time
import random
import json
import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import cloudscraper
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
import warnings
from dotenv import load_dotenv
from supabase import create_client

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

DEBUG_DIAGNOSTICS = True

def _dbg(msg):
    if DEBUG_DIAGNOSTICS:
        print(f"    [DEBUG] {msg}")

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise ValueError("SUPABASE_URL veya SUPABASE_SECRET_KEY bulunamadi!")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

RETAILERS = [
    {
        "name": "Gratis",
        "slug": "gratis",
        "start_urls": ["https://www.gratis.com/makyaj-c-100", "https://www.gratis.com/cilt-bakim-c-200"],
        "pagination_param": "page",
        "max_pages": 15,
        "concurrent_workers": 4
    },
    {
        "name": "Sephora",
        "slug": "sephora",
        "start_urls": ["https://www.sephora.com.tr/makyaj-c302/", "https://www.sephora.com.tr/cilt-bakim-c303/"],
        "pagination_param": "page",
        "max_pages": 15,
        "concurrent_workers": 1
    },
    {
        "name": "Boyner Beauty",
        "slug": "boyner",
        "start_urls": ["https://www.boyner.com.tr/kozmetik-c-10", "https://www.boyner.com.tr/parfum-c-1001"],
        "pagination_param": "page",
        "max_pages": 15,
        "use_playwright": True,
        "concurrent_workers": 1
    },
    {
        "name": "Kozmela",
        "slug": "kozmela",
        "start_urls": ["https://www.kozmela.com/cilt-bakimi", "https://www.kozmela.com/makyaj"],
        "pagination_param": "page",
        "max_pages": 15,
        "use_playwright": True,
        "concurrent_workers": 1
    }
]

CONCURRENT_WORKERS = 1
MAX_STORE_RUNTIME_SECONDS = int(os.getenv("MAX_STORE_RUNTIME_SECONDS", 60 * 40))

PLAYWRIGHT_CATEGORY_WAIT_MS = 2500
PLAYWRIGHT_PRODUCT_WAIT_MS = 1500
PLAYWRIGHT_NAV_TIMEOUT_MS = 20000

def get_scraper():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Sec-Ch-Ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    })
    return s

_thread_local = threading.local()

def get_thread_scraper(fresh=False):
    if fresh or not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = get_scraper()
    return _thread_local.scraper

_playwright_lock = threading.Lock()
_playwright_state = {"pw": None, "browser": None}

def _ensure_playwright_browser():
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright paketi kurulu degil.")
    with _playwright_lock:
        if _playwright_state["browser"] is None:
            _dbg("[playwright] Chromium baslatiliyor...")
            pw = sync_playwright().start()
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"]
            )
            _playwright_state["pw"] = pw
            _playwright_state["browser"] = browser
    return _playwright_state["browser"]

def close_playwright():
    with _playwright_lock:
        if _playwright_state["browser"] is not None:
            try:
                _playwright_state["browser"].close()
            except Exception:
                pass
        if _playwright_state["pw"] is not None:
            try:
                _playwright_state["pw"].stop()
            except Exception:
                pass
        _playwright_state["browser"] = None
        _playwright_state["pw"] = None

class _PlaywrightResponse:
    def __init__(self, text, status_code):
        self.text = text
        self.status_code = status_code if status_code is not None else 200
        self.headers = {}

def fetch_page_playwright(url, referer=None, wait_ms=2000, timeout_ms=None):
    timeout_ms = timeout_ms or PLAYWRIGHT_NAV_TIMEOUT_MS
    browser = _ensure_playwright_browser()
    with _playwright_lock:
        context = None
        try:
            extra_headers = {"Accept-Language": "tr-TR,tr;q=0.9"}
            if referer:
                extra_headers["Referer"] = referer
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
                locale="tr-TR",
                viewport={"width": 1366, "height": 900},
                extra_http_headers=extra_headers,
            )
            page = context.new_page()
            response = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            html = page.content()
            status = response.status if response else None
            return url, html, status, None
        except Exception as e:
            return url, None, None, str(e)
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

def fetch_product_page(url, referer=None, min_delay=1.0, max_delay=2.5, use_playwright=False):
    if use_playwright:
        _, html, status, err = fetch_page_playwright(url, referer=referer, wait_ms=PLAYWRIGHT_PRODUCT_WAIT_MS)
        if err or html is None:
            return url, None, err or "playwright: sayfa alinamadi"
        return url, _PlaywrightResponse(html, status), None

    time.sleep(random.uniform(min_delay, max_delay))
    try:
        scraper = get_thread_scraper()
        headers = {"Referer": referer} if referer else {}
        res = scraper.get(url, timeout=15, headers=headers)

        if res.status_code == 403:
            time.sleep(random.uniform(4.0, 8.0))
            scraper = get_thread_scraper(fresh=True)
            res = scraper.get(url, timeout=15, headers=headers)

        return url, res, None
    except Exception as e:
        return url, None, str(e)

def clean_text(val):
    if not val: return None
    val = str(val).replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    val = re.sub(r"\s+", " ", val).strip()
    return val if val else None

def fix_sephora_title(brand_name, product_name):
    if not brand_name or not product_name:
        return product_name
    if product_name.startswith(brand_name) and len(product_name) > len(brand_name):
        cleaned = product_name[len(brand_name):].strip()
        return cleaned if cleaned else product_name
    return product_name

def make_slug(text):
    if not text: return None
    text = str(text).lower()
    for old, new in [("ç", "c"), ("ğ", "g"), ("ı", "i"), ("ö", "o"), ("ş", "s"), ("ü", "u")]:
        text = text.replace(old, new)
    slug = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return slug if slug else f"product-{random.randint(1000, 99999)}"

def clean_price(val):
    if not val: return None
    raw = str(val).strip()
    cleaned = re.sub(r"[^\d,.]", "", raw)
    if not cleaned: return None

    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        price_float = float(cleaned)
        if 0 < price_float <= 150000.0:
            return price_float
        return None
    except ValueError:
        return None

def is_valid_ean13(code):
    if not code or len(code) != 13 or not code.isdigit():
        return False
    digits = [int(d) for d in code]
    checksum = sum(digits[i] * (1 if i % 2 == 0 else 3) for i in range(12))
    check_digit = (10 - (checksum % 10)) % 10
    return check_digit == digits[12]

def get_or_create_retailer(name, slug):
    res = supabase.table("retailers").select("id").eq("slug", slug).limit(1).execute()
    if res.data: return res.data[0]["id"]
    ins = supabase.table("retailers").insert({"name": name, "slug": slug}).execute()
    return ins.data[0]["id"]

_brand_cache = {}
_ingredient_cache = {}

def get_or_create_brand(brand_name):
    if not brand_name: brand_name = "Genel"
    brand_name = clean_text(brand_name)
    slug = make_slug(brand_name)

    if slug in _brand_cache:
        return _brand_cache[slug]

    res = supabase.table("brands").select("id").eq("slug", slug).limit(1).execute()
    if res.data:
        _brand_cache[slug] = res.data[0]["id"]
        return _brand_cache[slug]

    try:
        ins = supabase.table("brands").insert({"name": brand_name, "slug": slug}).execute()
        _brand_cache[slug] = ins.data[0]["id"]
        return _brand_cache[slug]
    except Exception as e:
        res2 = supabase.table("brands").select("id").eq("slug", slug).limit(1).execute()
        if res2.data:
            _brand_cache[slug] = res2.data[0]["id"]
            return _brand_cache[slug]
        return None

def save_ingredients(product_id, raw_inci):
    if not raw_inci: return
    items = [clean_text(i) for i in raw_inci.split(",") if clean_text(i)]
    for order, item in enumerate(items, start=1):
        try:
            if item in _ingredient_cache:
                ing_id = _ingredient_cache[item]
            else:
                res = supabase.table("ingredients").select("id").eq("inci_name", item).limit(1).execute()
                if res.data:
                    ing_id = res.data[0]["id"]
                else:
                    try:
                        ing_id = supabase.table("ingredients").insert({"inci_name": item}).execute().data[0]["id"]
                    except Exception:
                        res2 = supabase.table("ingredients").select("id").eq("inci_name", item).limit(1).execute()
                        if not res2.data: continue
                        ing_id = res2.data[0]["id"]
                _ingredient_cache[item] = ing_id

            supabase.table("product_ingredients").insert({
                "product_id": product_id,
                "ingredient_id": ing_id,
                "ingredient_order": order
            }).execute()
        except Exception as e:
            pass

def save_product_image(product_id, image_url):
    if not image_url: return
    try:
        supabase.table("product_images").insert({
            "product_id": product_id,
            "image_url": image_url,
            "is_primary": True,
            "sort_order": 1
        }).execute()
    except Exception as e:
        pass

def save_price(product_id, retailer_id, price, product_url):
    if price is None: return
    try:
        supabase.table("product_prices").insert({
            "product_id": product_id,
            "retailer_id": retailer_id,
            "price": price,
            "currency": "TRY",
            "product_url": product_url,
            "is_available": True
        }).execute()
    except Exception as e:
        pass

def parse_sitemap_url(sub_url, product_url_pattern, stats):
    found = set()
    try:
        scraper = get_thread_scraper()
        sub_res = scraper.get(sub_url, timeout=12)
        stats["status_codes"][f"sitemap:{sub_res.status_code}"] = stats["status_codes"].get(f"sitemap:{sub_res.status_code}", 0) + 1

        if sub_res.status_code == 200:
            sub_locs = re.findall(r"<loc>([^<]+)</loc>", sub_res.text)
            matched = [loc for loc in sub_locs if product_url_pattern.search(loc)]
            found.update(matched)
    except Exception as e:
        pass
    return found

def try_sitemap_urls(scraper, base_domain, stats):
    candidate_paths = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml"]
    product_url_pattern = re.compile(r"-p-|/p/|-pr-|/pr/|urun|product", re.IGNORECASE)
    found_urls = set()

    for path in candidate_paths:
        try:
            res = scraper.get(base_domain + path, timeout=12)
            stats["status_codes"][f"sitemap:{res.status_code}"] = stats["status_codes"].get(f"sitemap:{res.status_code}", 0) + 1

            if res.status_code != 200 or "xml" not in res.headers.get("Content-Type", "").lower():
                continue

            sub_sitemaps = re.findall(r"<loc>([^<]+\.xml[^<]*)</loc>", res.text)
            locs = re.findall(r"<loc>([^<]+)</loc>", res.text)

            matched_top = 0
            for loc in locs:
                if loc not in sub_sitemaps and product_url_pattern.search(loc):
                    found_urls.add(loc)
                    matched_top += 1

            relevant_sub = [s for s in sub_sitemaps if re.search(r"product|urun|category|kategori", s, re.IGNORECASE)] or sub_sitemaps[:15]

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(parse_sitemap_url, sub_url, product_url_pattern, stats) for sub_url in relevant_sub[:15]]
                for future in as_completed(futures):
                    found_urls.update(future.result())

            if found_urls: break
        except Exception as e:
            continue
    return list(found_urls)

CATEGORY_URL_BLOCKLIST = re.compile(
    r"(kategori|modelleri|koleksiyon|filtre=|sirala=|/c-\d|-c-\d+(?:/|$))",
    re.IGNORECASE
)

def _looks_like_product_url(href):
    if CATEGORY_URL_BLOCKLIST.search(href):
        return False
    return any(k in href for k in ["-p-", "/p/", "urun", "product", ".html", "-pr-", "/pr/", "/collections/"])

def extract_product_urls_from_category(scraper, cat_url, stats, pagination_param="page", max_pages=15, use_playwright=False):
    found_urls = set()
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        try:
            sep = "&" if "?" in cat_url else "?"
            page_url = cat_url if page == 1 else f"{cat_url}{sep}{pagination_param}={page}"

            if use_playwright:
                _, html, status, err = fetch_page_playwright(page_url, referer=cat_url, wait_ms=PLAYWRIGHT_CATEGORY_WAIT_MS)
                if err or html is None:
                    break
                status_key = status if status is not None else "pw:unknown"
                stats["status_codes"][status_key] = stats["status_codes"].get(status_key, 0) + 1
                res_text = html
            else:
                res = scraper.get(page_url, timeout=12)
                stats["status_codes"][res.status_code] = stats["status_codes"].get(res.status_code, 0) + 1
                if res.status_code != 200:
                    break
                res_text = res.text

            before_count = len(found_urls)
            soup = BeautifulSoup(res_text, "html.parser")

            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.startswith("javascript"):
                    continue
                if not _looks_like_product_url(href):
                    continue
                if href.startswith("/"):
                    base = "/".join(cat_url.split("/")[:3])
                    href = base + href
                found_urls.add(href)

            script_urls = re.findall(r'https?://[^\s"\'<>]+(?:-p-|-pr-|/p/|/pr/|urun)[^\s"\'<>]*', res_text)
            for u in script_urls:
                if _looks_like_product_url(u):
                    found_urls.add(u)

            new_count = len(found_urls) - before_count
            if new_count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2: break
            else:
                consecutive_empty = 0

            if not use_playwright:
                time.sleep(0.8)
        except Exception as e:
            break
    return list(found_urls)

PRICE_BLACKLIST_WORDS = [
    "kargo", "ücretsiz", "taksit", "üzeri", "kupon", "indirim kodu",
    "hediye çeki", "hediye kartı", "bakiye", "puan kazan", "başlayan fiyat",
    "kazandır", "kampanya"
]

def _is_valid_price_context(el):
    context = (el.get_text(" ", strip=True) or "").lower()
    parent = el.parent
    if parent is not None:
        context += " " + (parent.get_text(" ", strip=True) or "").lower()[:200]
    return not any(bad in context for bad in PRICE_BLACKLIST_WORDS)

BROAD_PRICE_REPEAT_THRESHOLD = 10
_broad_price_counts_lock = threading.Lock()

def _register_broad_price_and_check(price_counts, price):
    with _broad_price_counts_lock:
        count = price_counts.get(price, 0) + 1
        price_counts[price] = count
        return count <= BROAD_PRICE_REPEAT_THRESHOLD

INGREDIENT_BLACKLIST_WORDS = [
    "iade", "kolay i̇ade", "değerlendirme", "yorum", "kargo", "taksit",
    "hesabım", "sipariş", "ürün kodu", "ürün barkodu", "menşei",
    "anahtar kelimeler", "favoriledi", "satın al", "sepete ekle",
    "vergi", "banka kartı", "kredi kartı", "ticari ünvan", "posta adresi",
    "e-posta", "ithalatçı", "üretici firma", "değerlendir"
]

def _looks_like_ingredient_list(txt):
    if not txt or len(txt) < 10 or len(txt) > 1200:
        return False
    lower = txt.lower()
    if any(bad in lower for bad in INGREDIENT_BLACKLIST_WORDS):
        return False
    parts = [p.strip() for p in txt.split(",") if p.strip()]
    if len(parts) < 2:
        return False
    avg_len = sum(len(p) for p in parts) / len(parts)
    if avg_len > 45:
        return False
    if any(len(p.split()) > 8 for p in parts):
        return False
    return True

def extract_ingredients(soup):
    candidates = []
    for el in soup.find_all(["div", "p", "span", "li", "section"]):
        txt = clean_text(el.get_text())
        if not txt:
            continue
        if "İçindekiler" not in txt and "Ingredients" not in txt and "INCI" not in txt.upper():
            continue
        candidate = txt.replace("İçindekiler:", "").replace("Ingredients:", "").replace("İçindekiler", "").strip()
        candidate = clean_text(candidate)
        if _looks_like_ingredient_list(candidate):
            candidates.append(candidate)

    if not candidates:
        return None
    candidates.sort(key=len)
    return candidates[0]

KNOWN_NON_PRODUCT_NAMES = {
    "süpermarket", "markalar", "makyaj", "cilt bakım", "saç bakım",
    "temizleme ürünleri", "kağıt ürünleri", "tekstil ürünleri",
    "güneş ürünleri", "bebek banyo ürünleri", "kadın", "erkek", "bebek",
    "kozmetik", "kız çocuk", "erkek çocuk", "parfüm", "aksesuar"
}

CATEGORY_NAME_SUFFIX_PATTERN = re.compile(
    r"(modelleri|model[iİ]|ürünleri|ürünler|urunleri|urunler)\s*$",
    re.IGNORECASE
)

def parse_product_page(soup, p_res, price_counts=None):
    name, brand_name, price, image_url, inci_text = None, "Genel", None, None, None
    is_confirmed_product = False

    try:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string: continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product":
                    is_confirmed_product = True
                    name = clean_text(item.get("name"))
                    if "brand" in item:
                        brand_info = item["brand"]
                        brand_name = brand_info.get("name") if isinstance(brand_info, dict) else str(brand_info)

                    offers = item.get("offers", {})
                    if isinstance(offers, list) and offers: offers = offers[0]

                    raw_p = offers.get("price") or offers.get("lowPrice")
                    if raw_p: price = clean_price(raw_p)

                    image = item.get("image")
                    if isinstance(image, list) and image: image_url = image[0]
                    elif isinstance(image, str): image_url = image
                    break
            if name: break
    except Exception:
        pass

    if not name:
        h1 = soup.select_one("h1")
        if h1: name = clean_text(h1.get_text())

    if not name:
        meta_title = soup.select_one("meta[property='og:title']")
        if meta_title: name = clean_text(meta_title.get("content"))

    if not name: return None, None, None, None, None

    name_normalized = name.strip().lower().rstrip(".")
    word_count = len(name_normalized.split())
    if (
        name_normalized in KNOWN_NON_PRODUCT_NAMES
        or word_count <= 1
        or CATEGORY_NAME_SUFFIX_PATTERN.search(name_normalized)
    ):
        return None, None, None, None, None

    has_weak_cart_signal = True
    if not is_confirmed_product:
        has_cart_text = soup.find(string=re.compile(r"sepete ekle|add to cart|satın al", re.IGNORECASE))
        has_cart_button = soup.select_one("button[class*='cart'], button[class*='sepet'], [class*='add-to-cart']")
        has_weak_cart_signal = bool(has_cart_text or has_cart_button)
        if not has_weak_cart_signal and word_count <= 3:
            return None, None, None, None, None

    if not brand_name or brand_name == "Genel":
        brand_el = soup.select_one("[class*='brand'], [itemprop='brand'], .product-brand")
        if brand_el:
            brand_name = clean_text(brand_el.get_text())
        else:
            parts = name.split()
            if len(parts) > 1: brand_name = parts[0]

    name = fix_sephora_title(brand_name, name)

    if not price:
        broad_selector = "[class*='price']"
        priority_selectors = [
            ".price-sales", ".price-undiscounted", ".discount-price", ".current-price",
            "[class*='discounted']", "[itemprop='price']", "span[data-price]", broad_selector
        ]
        for sel in priority_selectors:
            for el in soup.select(sel):
                if not _is_valid_price_context(el):
                    continue
                p = clean_price(el.get_text())
                if not p:
                    continue
                if sel == broad_selector and price_counts is not None:
                    if not _register_broad_price_and_check(price_counts, p):
                        continue
                price = p
                break
            if price:
                break

    if not image_url:
        img_el = soup.select_one("meta[property='og:image'], [class*='product-image'] img")
        if img_el:
            candidate = img_el.get("content") or img_el.get("src")
            if candidate and "logo" not in candidate.lower():
                image_url = candidate

    if not inci_text:
        inci_text = extract_ingredients(soup)

    return name, brand_name, price, image_url, inci_text

def process_store(store):
    print(f"\n==================== {store['name']} Taranıyor ====================")
    use_playwright = store.get("use_playwright", False)
    if use_playwright:
        _dbg(f"[{store['name']}] Playwright modu AKTIF")

    scraper = get_scraper()
    retailer_id = get_or_create_retailer(store["name"], store["slug"])

    stats = {"status_codes": {}, "new_products": 0, "updated_prices": 0, "skipped": 0, "timed_out": False}

    pagination_param = store.get("pagination_param", "page")
    max_pages = store.get("max_pages", 15)

    base_domain = "https://" + store["start_urls"][0].split("/")[2]

    product_urls = set(try_sitemap_urls(scraper, base_domain, stats))

    if not product_urls:
        print(f"[{store['name']}] Sitemap bulunamadi, kategori sayfalari taranacak")
        for cat_url in store["start_urls"]:
            urls = extract_product_urls_from_category(
                scraper, cat_url, stats, pagination_param, max_pages, use_playwright=use_playwright
            )
            for u in urls: product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")

    idx = 0
    start_time = time.time()
    price_counts = {}
    workers = store.get("concurrent_workers", CONCURRENT_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_url = {
            executor.submit(fetch_product_page, url, referer=base_domain, use_playwright=use_playwright): url
            for url in product_urls
        }

        for future in as_completed(future_to_url):
            if time.time() - start_time > MAX_STORE_RUNTIME_SECONDS:
                stats["timed_out"] = True
                for f in future_to_url:
                    f.cancel()
                break

            url, p_res, error = future.result()
            idx += 1
            try:
                if error or p_res is None:
                    stats["skipped"] += 1
                    continue

                stats["status_codes"][p_res.status_code] = stats["status_codes"].get(p_res.status_code, 0) + 1
                if p_res.status_code != 200:
                    stats["skipped"] += 1
                    continue
                soup = BeautifulSoup(p_res.text, "html.parser")

                name, brand_name, price, image_url, inci_text = parse_product_page(soup, p_res, price_counts)
                if not name or price is None:
                    stats["skipped"] += 1
                    continue

                brand_id = get_or_create_brand(brand_name)
                category = "Kozmetik"
                barcode = None
                
                slug = make_slug(name)
                unique_slug = f"{slug}-{store['slug']}"
                existing = supabase.table("products").select("id, name").eq("slug", unique_slug).limit(1).execute()

                is_new_product = not existing.data

                if existing.data:
                    product_id = existing.data[0]["id"]
                else:
                    ins = supabase.table("products").insert({
                        "brand_id": brand_id,
                        "name": name,
                        "slug": unique_slug,
                        "category": category,
                        "barcode": barcode,
                        "image_url": image_url,
                        "original_inci_text": inci_text
                    }).execute()
                    product_id = ins.data[0]["id"] if ins.data else None

                if product_id:
                    if is_new_product:
                        save_ingredients(product_id, inci_text)
                        save_product_image(product_id, image_url)
                        stats["new_products"] += 1

                    save_price(product_id, retailer_id, price, url)
                    stats["updated_prices"] += 1

            except Exception as e:
                stats["skipped"] += 1
                continue

    return stats

def main():
    import sys
    target_slug = sys.argv[1] if len(sys.argv) > 1 else None
    stores_to_run = [s for s in RETAILERS if s["slug"] == target_slug] if target_slug else RETAILERS

    overall = {}
    try:
        for store in stores_to_run:
            try:
                overall[store["name"]] = process_store(store)
                time.sleep(2)
            except Exception as e:
                continue
    finally:
        close_playwright()

if __name__ == "__main__":
    main()
