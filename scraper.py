import os
import re
import time
import random
from datetime import datetime, timezone
import cloudscraper
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from supabase import create_client

# ============================================================
# 1. BAGLANTI VE AYARLAR
# ============================================================
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY")

if not SUPABASE_URL or not SUPABASE_SECRET_KEY:
    raise ValueError("SUPABASE_URL veya SUPABASE_SECRET_KEY bulunamadi!")

supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)

# Her mağaza için "pagination_param": site sayfalamayı hangi query parametresiyle
# yapıyor (bazıları ?page=N, bazıları ?p=N kullanıyor). "max_pages": kaç sayfaya
# kadar denenecek üst sınır - boş/tekrar eden sayfa gelirse zaten otomatik durulur.
RETAILERS = [
    {
        "name": "Gratis",
        "slug": "gratis",
        "start_urls": ["https://www.gratis.com/makyaj-c-100", "https://www.gratis.com/cilt-bakim-c-200"],
        "pagination_param": "page",
        "max_pages": 15
    },
    {
        "name": "Watsons",
        "slug": "watsons",
        # Duzeltilmis kategori ID'leri (eskisi 1010/1000 hatali idi -> 101/100)
        "start_urls": ["https://www.watsons.com.tr/cilt-bakim/c/101", "https://www.watsons.com.tr/makyaj/c/100"],
        "pagination_param": "currentPage",  # Watsons "page" degil "currentPage" kullaniyor
        "max_pages": 15
    },
    {
        "name": "Sephora",
        "slug": "sephora",
        # "/shop/" onekı hatali idi, gercek yapida yok
        "start_urls": ["https://www.sephora.com.tr/makyaj-c302/", "https://www.sephora.com.tr/cilt-bakim-c303/"],
        "pagination_param": "page",
        "max_pages": 15
    },
    {
        "name": "Sevil Parfümeri",
        "slug": "sevil",
        # ".html" uzantisi gercek sitede yok
        "start_urls": ["https://www.sevil.com.tr/parfum", "https://www.sevil.com.tr/cilt-vucut-sac-bakimi", "https://www.sevil.com.tr/makyaj"],
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
        "name": "Rossmann",
        "slug": "rossmann",
        # "-c-101" gibi ID'li url'ler gercek sitede yok, kategori duz isimle
        "start_urls": ["https://www.rossmann.com.tr/makyaj", "https://www.rossmann.com.tr/cilt-bakimi"],
        "pagination_param": "p",  # Rossmann ?p=N kullaniyor, ?page=N degil
        "max_pages": 15
    },
    {
        "name": "Eve Shop",
        "slug": "eveshop",
        # Eve Shop Shopify tabanli, kategoriler /collections/ altinda
        "start_urls": ["https://www.eveshop.com.tr/makyaj", "https://www.eveshop.com.tr/collections/cilt-bakimi"],
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

REQUEST_DELAY = 1.2

def get_scraper():
    s = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False}
    )
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
    })
    return s

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
    val = re.sub(r"[^\d,.]", "", str(val)).strip()
    if not val: return None
    if "," in val:
        val = val.replace(".", "").replace(",", ".")
    try:
        return float(val)
    except ValueError:
        return None

def is_valid_ean13(code):
    """EAN-13 checksum dogrulamasi - rastgele 13 haneli sayilari barkod sanmayi engeller."""
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

def get_or_create_brand(brand_name):
    if not brand_name: brand_name = "Genel"
    brand_name = clean_text(brand_name)
    slug = make_slug(brand_name)
    res = supabase.table("brands").select("id").eq("slug", slug).limit(1).execute()
    if res.data: return res.data[0]["id"]
    ins = supabase.table("brands").insert({"name": brand_name, "slug": slug}).execute()
    return ins.data[0]["id"]

def save_ingredients(product_id, raw_inci):
    """Sadece yeni urunler icin cagrilir - tekrar calistirmada duplicate satir olusturmaz."""
    if not raw_inci: return
    items = [clean_text(i) for i in raw_inci.split(",") if clean_text(i)]
    for order, item in enumerate(items, start=1):
        try:
            res = supabase.table("ingredients").select("id").eq("inci_name", item).limit(1).execute()
            ing_id = res.data[0]["id"] if res.data else supabase.table("ingredients").insert({"inci_name": item}).execute().data[0]["id"]
            supabase.table("product_ingredients").insert({
                "product_id": product_id,
                "ingredient_id": ing_id,
                "ingredient_order": order
            }).execute()
        except Exception:
            pass

def save_product_image(product_id, image_url):
    """Sadece yeni urunler icin cagrilir - tekrar calistirmada duplicate gorsel eklenmez."""
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
    """Her calistirmada cagrilir - fiyat gecmisi kasitli olarak biriktirilir."""
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

def extract_product_urls_from_category(scraper, cat_url, stats, pagination_param="page", max_pages=15):
    """
    Kategori sayfalarini gezerek urun linklerini toplar.
    - pagination_param: magazaya gore degisen sayfalama sorgu parametresi (page/p/vb.)
    - max_pages: ust sinir; bir sayfada YENI urun linki bulunamazsa (once bulunanlarla
      ayni kumeyse) veya sayfa 404/hata donerse dongu erken sonlandirilir, boylece
      kucuk kataloglarda gereksiz istek atilmaz, buyuk kataloglarda ise tum sayfalar taranir.
    """
    found_urls = set()
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        try:
            sep = "&" if "?" in cat_url else "?"
            page_url = cat_url if page == 1 else f"{cat_url}{sep}{pagination_param}={page}"
            res = scraper.get(page_url, timeout=12)
            stats["status_codes"][res.status_code] = stats["status_codes"].get(res.status_code, 0) + 1

            if res.status_code != 200:
                break

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
                # Bir sayfa hic yeni link getirmediyse (kategori bitti ya da
                # sayfalama parametresi sitede etkisiz) 2 sayfa daha deneyip durur.
                if consecutive_empty >= 2:
                    break
            else:
                consecutive_empty = 0

            time.sleep(0.5)
        except Exception:
            break

    return list(found_urls)

def parse_product_page(soup, p_res):
    """Genel/esnek urun adi, marka ve fiyat cikarma (magazaya ozel degil, ortak fallback)."""
    # Urun Adi
    name = None
    h1 = soup.select_one("h1, .product-name, [class*='product-title'], [class*='title']")
    if h1: name = clean_text(h1.get_text())

    if not name:
        meta_title = soup.select_one("meta[property='og:title']")
        if meta_title: name = clean_text(meta_title.get("content"))

    if not name: return None, None, None, None, None

    # Marka
    brand_name = "Genel"
    brand_el = soup.select_one("[class*='brand'], [itemprop='brand'], .product-brand")
    if brand_el:
        brand_name = clean_text(brand_el.get_text())
    else:
        parts = name.split()
        if len(parts) > 1: brand_name = parts[0]

    # Fiyat - indirimli/guncel fiyat secicilerine oncelik verilir
    price = None
    priority_selectors = [
        ".discount-price", ".current-price", "[class*='discounted']",
        "[itemprop='price']", "span[data-price]", "[class*='price']"
    ]
    for sel in priority_selectors:
        el = soup.select_one(sel)
        if el:
            p = clean_price(el.get_text())
            if p:
                price = p
                break

    if not price:
        matches = re.findall(r'(\d+[\.,]?\d*)\s*(?:TL|₺)', p_res.text)
        if matches:
            for m in matches:
                p = clean_price(m)
                if p and p > 5:
                    price = p
                    break

    # Gorsel
    img_el = soup.select_one("meta[property='og:image'], [class*='product-image'] img")
    image_url = None
    if img_el:
        image_url = img_el.get("content") or img_el.get("src")

    # INCI Maddeleri
    inci_text = None
    for el in soup.find_all(["div", "p", "span", "section"]):
        txt = el.get_text()
        if ("İçindekiler" in txt or "Ingredients" in txt) and len(txt) > 30 and "," in txt:
            inci_text = clean_text(txt.replace("İçindekiler:", "").replace("Ingredients:", ""))
            break

    return name, brand_name, price, image_url, inci_text

def process_store(store):
    print(f"\n==================== {store['name']} Taranıyor ====================")
    scraper = get_scraper()
    retailer_id = get_or_create_retailer(store["name"], store["slug"])

    stats = {"status_codes": {}, "new_products": 0, "updated_prices": 0, "skipped": 0}

    pagination_param = store.get("pagination_param", "page")
    max_pages = store.get("max_pages", 15)

    product_urls = set()
    for cat_url in store["start_urls"]:
        urls = extract_product_urls_from_category(scraper, cat_url, stats, pagination_param, max_pages)
        print(f"[{store['name']}] {cat_url} -> {len(urls)} link bulundu")
        for u in urls:
            product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")

    for idx, url in enumerate(product_urls, start=1):
        try:
            p_res = scraper.get(url, timeout=12)
            stats["status_codes"][p_res.status_code] = stats["status_codes"].get(p_res.status_code, 0) + 1
            if p_res.status_code != 200:
                stats["skipped"] += 1
                continue
            soup = BeautifulSoup(p_res.text, "html.parser")

            name, brand_name, price, image_url, inci_text = parse_product_page(soup, p_res)
            if not name:
                stats["skipped"] += 1
                continue

            brand_id = get_or_create_brand(brand_name)

            category = "Kozmetik"
            lower_name = name.lower()
            if any(w in lower_name for w in ["krem", "nemlendirici", "serum", "tonik", "temizleyici"]): category = "Cilt Bakımı"
            elif any(w in lower_name for w in ["parfüm", "edt", "edp", "deodorant"]): category = "Parfüm"
            elif any(w in lower_name for w in ["şampuan", "saç kremi", "maske", "saç yağ"]): category = "Saç Bakımı"
            elif any(w in lower_name for w in ["ruj", "fondöten", "maskara", "allık", "kapatıcı"]): category = "Makyaj"

            # Barkod: sadece EAN-13 checksum'ini gecen sayilar kabul edilir
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
                # Malzeme ve gorsel sadece YENI urunlerde eklenir -> tekrar calistirmada
                # ayni satirlarin coklanmasi (duplicate) onlenir.
                if is_new_product:
                    save_ingredients(product_id, inci_text)
                    save_product_image(product_id, image_url)
                    stats["new_products"] += 1

                # Fiyat her calistirmada eklenir -> fiyat gecmisi bilincli olarak birikir.
                save_price(product_id, retailer_id, price, url)
                stats["updated_prices"] += 1
                tag = "YENİ" if is_new_product else "GÜNCEL"
                print(f"[{store['name']}] [{idx}/{len(product_urls)}] {tag}: {name[:30]}... | {price} TL")

            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"[{store['name']}] Hata: {e}")
            stats["skipped"] += 1
            continue

    print(f"[{store['name']}] ÖZET -> Yeni: {stats['new_products']} | Fiyat güncellenen: {stats['updated_prices']} | Atlanan: {stats['skipped']} | Status kodları: {stats['status_codes']}")
    return stats

def main():
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")
    overall = {}
    for store in RETAILERS:
        try:
            overall[store["name"]] = process_store(store)
            time.sleep(3)  # Mağazalar arası 3 saniye dinlenme (Ban koruması)
        except Exception as e:
            print(f"{store['name']} atlandı: {e}")
            continue

    print("\n==================== GENEL ÖZET ====================")
    for name, s in overall.items():
        print(f"{name}: Yeni={s['new_products']} FiyatGüncel={s['updated_prices']} Atlanan={s['skipped']} Status={s['status_codes']}")
    print("\nTüm Mağazaların Taranması Tamamlandı!")

if __name__ == "__main__":
    main()
