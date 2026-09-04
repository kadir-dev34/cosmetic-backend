import os
import re
import time
import random
import json
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
import cloudscraper
from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
import warnings
from dotenv import load_dotenv
from supabase import create_client

# XML ve BeautifulSoup Uyarılarını Gizle
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
warnings.filterwarnings("ignore", message=".*XMLParsedAsHTMLWarning.*")

# ============================================================
# 1. BAGLANTI VE AYARLAR
# ============================================================
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
        "max_pages": 15
    },
    {
        "name": "Sephora",
        "slug": "sephora",
        "start_urls": ["https://www.sephora.com.tr/makyaj-c302/", "https://www.sephora.com.tr/cilt-bakim-c303/"],
        "pagination_param": "page",
        "max_pages": 15
    },
    {
        "name": "Boyner Beauty",
        "slug": "boyner",
        "start_urls": ["https://www.boyner.com.tr/kozmetik-c-10", "https://www.boyner.com.tr/parfum-c-1001"],
        "pagination_param": "page",
        "max_pages": 15
    },
    {
        "name": "Kozmela",
        "slug": "kozmela",
        "start_urls": ["https://www.kozmela.com/cilt-bakimi", "https://www.kozmela.com/makyaj"],
        "pagination_param": "page",
        "max_pages": 15
    }
]

# Sephora'nın hassas koruması nedeniyle 1 worker kullanıyoruz
CONCURRENT_WORKERS = 1  

def get_scraper():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    # Gerçek Masaüstü Chrome Parmak İzi (Cloudflare By-Pass)
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
        "Sec-Ch-Ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "Sec-Ch-Ua-Mobile": "?0",
        "Sec-Ch-Ua-Platform": '"Windows"',
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1"
    })
    return s

_thread_local = threading.local()

def get_thread_scraper():
    if not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = get_scraper()
    return _thread_local.scraper

def fetch_product_page(url):
    # İnsansı rastgele gecikme (1.0 - 2.2 saniye)
    time.sleep(random.uniform(1.0, 2.2))
    try:
        scraper = get_thread_scraper()
        res = scraper.get(url, timeout=15)
        return url, res, None
    except Exception as e:
        return url, None, str(e)

def clean_text(val):
    if not val: return None
    val = str(val).replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    val = re.sub(r"\s+", " ", val).strip()
    return val if val else None

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
    
    # Türkçe fiyat formatını standartlaştırma (Örn: 1.250,50 -> 1250.50)
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")
        
    try:
        price_float = float(cleaned)
        # Sadece çok uçuk (150.000 TL üstü) fiyatları engelle, alt sınır YOK
        if price_float <= 150000.0:
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
    except Exception:
        res2 = supabase.table("brands").select("id").eq("slug", slug).limit(1).execute()
        if res2.data:
            _brand_cache[slug] = res2.data[0]["id"]
            return _brand_cache[slug]
        raise

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
                        if not res2.data: raise
                        ing_id = res2.data[0]["id"]
                _ingredient_cache[item] = ing_id
            supabase.table("product_ingredients").insert({
                "product_id": product_id,
                "ingredient_id": ing_id,
                "ingredient_order": order
            }).execute()
        except Exception:
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
    except Exception:
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
    except Exception:
        pass

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

            for loc in locs:
                if loc not in sub_sitemaps and product_url_pattern.search(loc):
                    found_urls.add(loc)

            relevant_sub = [s for s in sub_sitemaps if re.search(r"product|urun|category|kategori", s, re.IGNORECASE)] or sub_sitemaps[:15]
            for sub_url in relevant_sub[:15]:
                try:
                    sub_res = scraper.get(sub_url, timeout=12)
                    stats["status_codes"][f"sitemap:{sub_res.status_code}"] = stats["status_codes"].get(f"sitemap:{sub_res.status_code}", 0) + 1
                    if sub_res.status_code == 200:
                        sub_locs = re.findall(r"<loc>([^<]+)</loc>", sub_res.text)
                        for loc in sub_locs:
                            if product_url_pattern.search(loc):
                                found_urls.add(loc)
                    time.sleep(0.3)
                except Exception:
                    continue

            if found_urls: break
        except Exception:
            continue

    return list(found_urls)

def extract_product_urls_from_category(scraper, cat_url, stats, pagination_param="page", max_pages=15):
    found_urls = set()
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        try:
            sep = "&" if "?" in cat_url else "?"
            page_url = cat_url if page == 1 else f"{cat_url}{sep}{pagination_param}={page}"
            res = scraper.get(page_url, timeout=12)
            stats["status_codes"][res.status_code] = stats["status_codes"].get(res.status_code, 0) + 1

            if res.status_code != 200: break

            before_count = len(found_urls)
            soup = BeautifulSoup(res.text, "html.parser")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(k in href for k in ["-p-", "/p/", "urun", "product", ".html", "-pr-", "/pr/", "/collections/"]) and not href.startswith("javascript"):
                    if href.startswith("/"):
                        base = "/".join(cat_url.split("/")[:3])
                        href = base + href
                    found_urls.add(href)

            script_urls = re.findall(r'https?://[^\s"\'<>]+(?:-p-|-pr-|/p/|/pr/|urun)[^\s"\'<>]*', res.text)
            for u in script_urls:
                found_urls.add(u)

            new_count = len(found_urls) - before_count
            if new_count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2: break
            else:
                consecutive_empty = 0

            time.sleep(0.5)
        except Exception:
            break

    return list(found_urls)

def fetch_shopify_products_json(scraper, base_domain, stats, max_pages=60):
    all_products = []
    for page in range(1, max_pages + 1):
        url = f"{base_domain}/products.json?limit=250&page={page}"
        try:
            res = scraper.get(url, timeout=15)
            stats["status_codes"][f"shopify_json:{res.status_code}"] = stats["status_codes"].get(f"shopify_json:{res.status_code}", 0) + 1
            if res.status_code != 200: break
            data = res.json()
            products = data.get("products", [])
            if not products: break
            all_products.extend(products)
            if len(products) < 250: break
            time.sleep(0.3)
        except Exception:
            break
    return all_products

def parse_shopify_product(product, base_domain):
    name = clean_text(product.get("title"))
    if not name: return None

    brand_name = clean_text(product.get("vendor")) or "Genel"

    price = None
    variants = product.get("variants", []) or []
    variant_prices = [clean_price(v.get("price")) for v in variants]
    variant_prices = [p for p in variant_prices if p]
    if variant_prices:
        price = min(variant_prices)

    image_url = None
    images = product.get("images", []) or []
    if images:
        candidate = images[0].get("src")
        if candidate and "logo" not in candidate.lower():
            image_url = candidate

    handle = product.get("handle", "")
    product_url = f"{base_domain}/products/{handle}"

    barcode = None
    for v in variants:
        candidate_bc = v.get("barcode")
        if candidate_bc and is_valid_ean13(candidate_bc.strip()):
            barcode = candidate_bc.strip()
            break

    inci_text = None
    body_html = product.get("body_html") or ""
    if body_html:
        body_text = BeautifulSoup(body_html, "html.parser").get_text()
        for marker in ["İçindekiler", "Ingredients"]:
            idx = body_text.find(marker)
            if idx != -1:
                snippet = body_text[idx:idx + 2000]
                snippet = snippet.replace("İçindekiler:", "").replace("Ingredients:", "")
                if "," in snippet:
                    inci_text = clean_text(snippet)
                break

    return name, brand_name, price, image_url, product_url, barcode, inci_text

def parse_product_page(soup, p_res):
    # og:type kontrolü tamamen kaldırıldı – artık sadece isim ve fiyat varlığına göre karar verilecek

    name, brand_name, price, image_url, inci_text = None, "Genel", None, None, None

    # 1. YÖNTEM: JSON-LD Parsing (Boyner, Sephora ve modern siteler için)
    try:
        scripts = soup.find_all("script", type="application/ld+json")
        for script in scripts:
            if not script.string: continue
            data = json.loads(script.string)
            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "Product":
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

    # 2. YÖNTEM: HTML Fallback Taraması
    if not name:
        h1 = soup.select_one("h1")
        if h1: name = clean_text(h1.get_text())

    if not name:
        meta_title = soup.select_one("meta[property='og:title']")
        if meta_title: name = clean_text(meta_title.get("content"))

    if not name:
        fallback_el = soup.select_one(".product-name, [class*='product-title'], [class*='title']")
        if fallback_el: name = clean_text(fallback_el.get_text())

    if not name: return None, None, None, None, None

    # Kategori/Menü kelimelerini filtreleme
    KNOWN_NON_PRODUCT_NAMES = {
        "süpermarket", "markalar", "makyaj", "cilt bakım", "saç bakım",
        "temizleme ürünleri", "kağıt ürünleri", "tekstil ürünleri",
        "güneş ürünleri", "bebek banyo ürünleri", "kadın", "erkek", "bebek",
        "kozmetik", "kız çocuk", "erkek çocuk", "parfüm", "aksesuar",
        "kampanyalar", "indirim", "yeni ürünler", "çok satanlar"
    }
    name_normalized = name.strip().lower().rstrip(".")
    if name_normalized in KNOWN_NON_PRODUCT_NAMES or len(name_normalized.split()) <= 1:
        return None, None, None, None, None

    # Marka Fallback
    if not brand_name or brand_name == "Genel":
        brand_el = soup.select_one("[class*='brand'], [itemprop='brand'], .product-brand")
        if brand_el:
            brand_name = clean_text(brand_el.get_text())
        else:
            parts = name.split()
            if len(parts) > 1: brand_name = parts[0]

    # Fiyat Fallback
    if not price:
        priority_selectors = [
            ".price-sales", ".price-undiscounted", ".discount-price", ".current-price",
            "[class*='discounted']", "[itemprop='price']", "span[data-price]", "[class*='price']"
        ]
        for sel in priority_selectors:
            el = soup.select_one(sel)
            if el:
                p = clean_price(el.get_text())
                if p:
                    price = p
                    break

    # Görsel Fallback
    if not image_url:
        img_el = soup.select_one("meta[property='og:image'], [class*='product-image'] img")
        if img_el:
            candidate = img_el.get("content") or img_el.get("src")
            if candidate and "logo" not in candidate.lower():
                image_url = candidate

    # INCI Maddeleri
    inci_text = None
    for el in soup.find_all(["div", "p", "span", "section"]):
        txt = el.get_text()
        if ("İçindekiler" in txt or "Ingredients" in txt) and len(txt) > 30 and "," in txt:
            inci_text = clean_text(txt.replace("İçindekiler:", "").replace("Ingredients:", ""))
            break

    return name, brand_name, price, image_url, inci_text

def process_store_shopify(store):
    print(f"\n==================== {store['name']} Taranıyor (Shopify API) ====================")
    scraper = get_scraper()
    retailer_id = get_or_create_retailer(store["name"], store["slug"])
    stats = {"status_codes": {}, "new_products": 0, "updated_prices": 0, "skipped": 0}

    base_domain = "https://" + store["start_urls"][0].split("/")[2]
    products = fetch_shopify_products_json(scraper, base_domain, stats)
    print(f"[{store['name']}] Shopify API uzerinden {len(products)} urun bulundu")

    for idx, product in enumerate(products, start=1):
        try:
            parsed = parse_shopify_product(product, base_domain)
            if not parsed:
                stats["skipped"] += 1
                continue
            name, brand_name, price, image_url, product_url, barcode, inci_text = parsed

            brand_id = get_or_create_brand(brand_name)

            category = "Kozmetik"
            lower_name = name.lower()
            if any(w in lower_name for w in ["krem", "nemlendirici", "serum", "tonik", "temizleyici"]): category = "Cilt Bakımı"
            elif any(w in lower_name for w in ["parfüm", "edt", "edp", "deodorant"]): category = "Parfüm"
            elif any(w in lower_name for w in ["şampuan", "saç kremi", "maske", "saç yağ"]): category = "Saç Bakımı"
            elif any(w in lower_name for w in ["ruj", "fondöten", "maskara", "allık", "kapatıcı"]): category = "Makyaj"

            slug = make_slug(name)
            unique_slug = f"{slug}-{store['slug']}"
            existing = supabase.table("products").select("id").eq("slug", unique_slug).limit(1).execute()
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
                save_price(product_id, retailer_id, price, product_url)
                stats["updated_prices"] += 1
                tag = "YENİ" if is_new_product else "GÜNCEL"
                print(f"[{store['name']}] [{idx}/{len(products)}] {tag}: {name[:30]}... | {price} TL")
                if idx % 50 == 0:
                    print(f"[{store['name']}] --- İLERLEME: {idx}/{len(products)} işlendi ---")
        except Exception as e:
            print(f"[{store['name']}] Hata: {e}")
            stats["skipped"] += 1
            continue

    print(f"[{store['name']}] ÖZET -> Yeni: {stats['new_products']} | Fiyat güncellenen: {stats['updated_prices']} | Atlanan: {stats['skipped']} | Status kodları: {stats['status_codes']}")
    return stats

def process_store(store):
    print(f"\n==================== {store['name']} Taranıyor ====================")
    scraper = get_scraper()
    retailer_id = get_or_create_retailer(store["name"], store["slug"])

    stats = {"status_codes": {}, "new_products": 0, "updated_prices": 0, "skipped": 0}

    pagination_param = store.get("pagination_param", "page")
    max_pages = store.get("max_pages", 15)

    base_domain = "https://" + store["start_urls"][0].split("/")[2]
    product_urls = set(try_sitemap_urls(scraper, base_domain, stats))

    if not product_urls:
        print(f"[{store['name']}] Sitemap bulunamadi, kategori sayfalari taranacak")
        for cat_url in store["start_urls"]:
            urls = extract_product_urls_from_category(scraper, cat_url, stats, pagination_param, max_pages)
            for u in urls: product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")

    idx = 0
    with ThreadPoolExecutor(max_workers=CONCURRENT_WORKERS) as executor:
        future_to_url = {executor.submit(fetch_product_page, url): url for url in product_urls}

        for future in as_completed(future_to_url):
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

                name, brand_name, price, image_url, inci_text = parse_product_page(soup, p_res)
                if not name or price is None:
                    stats["skipped"] += 1
                    continue

                brand_id = get_or_create_brand(brand_name)

                category = "Kozmetik"
                lower_name = name.lower()
                if any(w in lower_name for w in ["krem", "nemlendirici", "serum", "tonik", "temizleyici"]): category = "Cilt Bakımı"
                elif any(w in lower_name for w in ["parfüm", "edt", "edp", "deodorant"]): category = "Parfüm"
                elif any(w in lower_name for w in ["şampuan", "saç kremi", "maske", "saç yağ"]): category = "Saç Bakımı"
                elif any(w in lower_name for w in ["ruj", "fondöten", "maskara", "allık", "kapatıcı"]): category = "Makyaj"

                barcode = None
                for candidate in re.findall(r"\b\d{13}\b", p_res.text):
                    if is_valid_ean13(candidate):
                        barcode = candidate
                        break

                slug = make_slug(name)
                unique_slug = f"{slug}-{store['slug']}"
                existing = supabase.table("products").select("id").eq("slug", unique_slug).limit(1).execute()

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
                    tag = "YENİ" if is_new_product else "GÜNCEL"
                    print(f"[{store['name']}] [{idx}/{len(product_urls)}] {tag}: {name[:30]}... | {price} TL")

            except Exception as e:
                print(f"[{store['name']}] Hata: {e}")
                stats["skipped"] += 1
                continue

    print(f"[{store['name']}] ÖZET -> Yeni: {stats['new_products']} | Fiyat güncellenen: {stats['updated_prices']} | Atlanan: {stats['skipped']} | Status kodları: {stats['status_codes']}")
    return stats

def main():
    import sys
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")

    target_slug = sys.argv[1] if len(sys.argv) > 1 else None

    if target_slug:
        stores_to_run = [s for s in RETAILERS if s["slug"] == target_slug]
        if not stores_to_run:
            valid_slugs = ", ".join(s["slug"] for s in RETAILERS)
            print(f"HATA: '{target_slug}' adinda bir magaza bulunamadi. Gecerli degerler: {valid_slugs}")
            sys.exit(1)
    else:
        stores_to_run = RETAILERS

    overall = {}
    for store in stores_to_run:
        try:
            if store.get("platform") == "shopify":
                overall[store["name"]] = process_store_shopify(store)
            else:
                overall[store["name"]] = process_store(store)
            time.sleep(3)
        except Exception as e:
            print(f"{store['name']} atlandı: {e}")
            continue

    print("\n==================== GENEL ÖZET ====================")
    for name, s in overall.items():
        print(f"{name}: Yeni={s['new_products']} FiyatGüncel={s['updated_prices']} Atlanan={s['skipped']} Status={s['status_codes']}")
    print("\nTaranma Tamamlandı!")

if __name__ == "__main__":
    main()
