import os
import re
import time
import random
import threading
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
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
#
# NOT: Watsons, Sevil Parfumeri, Rossmann ve Eve Shop listeden CIKARILDI.
# Bu 4 magaza hem GitHub'in bulut IP'sinden hem sizin ev IP'nizden (self-hosted
# runner testi) ayni sekilde 403 (erisim engeli) donduruyordu - hem sitemap.xml
# hem kategori sayfalari hem (Eve Shop icin) resmi Shopify API'si denendi, hicbiri
# calismadi. Bu, IP'den bagimsiz, tarayici "parmak izi" tabanli bir bot korumasi
# oldugunu gosteriyor ve mevcut yontemlerle asilamiyor. Asagidaki 4 magaza ise
# sorunsuz calisiyor.
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
        # "/shop/" onekı hatali idi, gercek yapida yok
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

CONCURRENT_WORKERS = 6  # Ayni anda kac urun sayfasi indirilecek (paralel indirme)

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

# Her thread kendi cloudscraper oturumunu kullanir (paylasilan tek oturumun
# es zamanli isteklerde beklenmedik hatalara yol acmasini onlemek icin).
_thread_local = threading.local()

def get_thread_scraper():
    if not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = get_scraper()
    return _thread_local.scraper

def fetch_product_page(url):
    """Paralel calisan worker'larin cagirdigi fonksiyon: bir urun sayfasini indirir.
    Kucuk rastgele bir gecikme (jitter) icerir - bu, 6 paralel worker'in bile
    tamamen bosluksuz/robotik bir istek paterni olusturmasini engeller ve
    bot korumasi sistemlerinin (Cloudflare, WAF vb.) "burst" trafigi olarak
    isaretleme riskini azaltir. Hizin buyuk kismi (paralellikten gelen) korunur."""
    time.sleep(random.uniform(0.3, 0.7))
    try:
        scraper = get_thread_scraper()
        res = scraper.get(url, timeout=12)
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

# Marka ve malzeme isim->id eslesmeleri bellekte tutulur, boylece ayni marka/
# malzeme icin tekrar tekrar veritabani sorgusu atilmaz. Yazma islemleri tek
# thread'de (ana thread) yapildigi icin bu sozlukler guvenle paylasilabilir.
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
        # Ayni anda calisan baska bir magazanin islemi bu markayi bizden once
        # eklemis olabilir (paralel calisma nedeniyle). Bu durumda hata vermek
        # yerine tekrar sorgulayip mevcut kaydi kullaniriz.
        res2 = supabase.table("brands").select("id").eq("slug", slug).limit(1).execute()
        if res2.data:
            _brand_cache[slug] = res2.data[0]["id"]
            return _brand_cache[slug]
        raise

def save_ingredients(product_id, raw_inci):
    """Sadece yeni urunler icin cagrilir - tekrar calistirmada duplicate satir olusturmaz."""
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
                        # Ayni anda calisan baska bir magaza ayni malzemeyi eklemis
                        # olabilir - tekrar sorgulayip mevcut kaydi kullan.
                        res2 = supabase.table("ingredients").select("id").eq("inci_name", item).limit(1).execute()
                        if not res2.data:
                            raise
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

def try_sitemap_urls(scraper, base_domain, stats):
    """
    Kategori sayfalarini tahmin etmek yerine, once sitenin sitemap.xml'ini
    dener. Cogu ciddi e-ticaret sitesi SEO icin TUM urun URL'lerini sitemap'te
    yayinlar - bu hem kategori/sayfalama tahmini gerektirmez hem de kategori
    bazli taramanin kacirdigi urunleri de yakalar.
    Basarili olursa urun-benzeri URL listesi doner, olmazsa bos liste doner
    (bu durumda cagiran taraf eski kategori tarama yontemine devam eder).
    """
    candidate_paths = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap/sitemap.xml"]
    product_url_pattern = re.compile(r"-p-|/p/|-pr-|/pr/|urun|product", re.IGNORECASE)
    found_urls = set()

    for path in candidate_paths:
        try:
            res = scraper.get(base_domain + path, timeout=12)
            stats["status_codes"][f"sitemap:{res.status_code}"] = stats["status_codes"].get(f"sitemap:{res.status_code}", 0) + 1
            if res.status_code != 200 or "xml" not in res.headers.get("Content-Type", "").lower():
                continue

            # Sitemap index ise (alt sitemap'lere link veriyorsa) once onlari topla
            sub_sitemaps = re.findall(r"<loc>([^<]+\.xml[^<]*)</loc>", res.text)
            locs = re.findall(r"<loc>([^<]+)</loc>", res.text)

            # Dogrudan urun sayfasi gibi gorunen loc'lari al
            for loc in locs:
                if loc not in sub_sitemaps and product_url_pattern.search(loc):
                    found_urls.add(loc)

            # Eger bu bir sitemap index ise, urun/kategori icerebilecek alt
            # sitemap'leri (en fazla 15 tanesini, asiri istek atmamak icin) gez
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

            if found_urls:
                break  # bu sitemap yolu calisti, digerlerini denemeye gerek yok
        except Exception:
            continue

    return list(found_urls)

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

def fetch_shopify_products_json(scraper, base_domain, stats, max_pages=60):
    """
    Shopify magazalari icin resmi/genel /products.json API'sini kullanir.
    HTML parse etmeye gerek kalmadan, TUM urunleri (isim, marka, fiyat, gorsel,
    barkod, aciklama) tek seferde temiz JSON olarak doner. HTML kazimadan
    (og:image, h1 tahmini vb.) cok daha guvenilir - yanlis logo/kategori-ismi
    gibi sorunlar bu yontemde olusmaz.
    """
    all_products = []
    for page in range(1, max_pages + 1):
        url = f"{base_domain}/products.json?limit=250&page={page}"
        try:
            res = scraper.get(url, timeout=15)
            stats["status_codes"][f"shopify_json:{res.status_code}"] = stats["status_codes"].get(f"shopify_json:{res.status_code}", 0) + 1
            if res.status_code != 200:
                break
            data = res.json()
            products = data.get("products", [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < 250:
                break  # son sayfaya gelindi
            time.sleep(0.3)
        except Exception:
            break
    return all_products

def parse_shopify_product(product, base_domain):
    """Shopify JSON'undan tek bir urunun alanlarini cikarir."""
    name = clean_text(product.get("title"))
    if not name:
        return None

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

    # Barkod: Shopify'da varyantlarda dogrudan barkod alani bulunur -
    # regex tahmini yerine gercek veri kullanilir, EAN-13 ise dogrulanir.
    barcode = None
    for v in variants:
        candidate_bc = v.get("barcode")
        if candidate_bc and is_valid_ean13(candidate_bc.strip()):
            barcode = candidate_bc.strip()
            break

    # Icerik/INCI - urun aciklamasi (body_html) icinde aranir
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
    """Genel/esnek urun adi, marka ve fiyat cikarma (magazaya ozel degil, ortak fallback)."""
    # Urun Adi - ONCELIK SIRASI ONEMLI: bazi sitelerde (orn. Kozmela) sayfanin
    # ust menusunde "[class*='title']" ile eslesen bir baslik (orn. "Markalar"
    # menu basligi) gercek urun basligindan ONCE gelebiliyor. select_one tek
    # bir cagrida BIRDEN FAZLA secici verilirse belgedeki ILK eslesmeyi
    # doner - secici sirasina degil, belge sirasina bakar. Bu yuzden en
    # guvenilir olan "h1" once TEK BASINA denenir, sonra og:title (cok
    # guvenilir), en son daha riskli class tabanli secicilere dusulur.
    name = None
    h1 = soup.select_one("h1")
    if h1: name = clean_text(h1.get_text())

    if not name:
        meta_title = soup.select_one("meta[property='og:title']")
        if meta_title: name = clean_text(meta_title.get("content"))

    if not name:
        fallback_el = soup.select_one(".product-name, [class*='product-title'], [class*='title']")
        if fallback_el: name = clean_text(fallback_el.get_text())

    if not name: return None, None, None, None, None

    # Kategori/menu sayfalarini urun sanip kaydetmeyi onlemek icin sağlamlık
    # kontrolu: bilinen genel kategori/menu isimleri veya tek kelimelik supheli
    # isimler gercek urun degildir, atlanir.
    KNOWN_NON_PRODUCT_NAMES = {
        "süpermarket", "markalar", "makyaj", "cilt bakım", "saç bakım",
        "temizleme ürünleri", "kağıt ürünleri", "tekstil ürünleri",
        "güneş ürünleri", "bebek banyo ürünleri", "kadın", "erkek", "bebek",
        "kozmetik", "kız çocuk", "erkek çocuk", "parfüm", "aksesuar",
        "kampanyalar", "indirim", "yeni ürünler", "çok satanlar"
    }
    name_normalized = name.strip().lower().rstrip(".")
    word_count = len(name_normalized.split())
    if name_normalized in KNOWN_NON_PRODUCT_NAMES or word_count <= 1:
        return None, None, None, None, None

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

    # Gorsel - site logosu yanlislikla urun gorseli olarak kaydedilmesin
    img_el = soup.select_one("meta[property='og:image'], [class*='product-image'] img")
    image_url = None
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
    """
    Shopify magazalari icin ozel islem akisi: HTML kazima yerine dogrudan
    /products.json API'sinden temiz veri cekilir. Tek bir cagri seti tum
    urun bilgilerini (isim, marka, fiyat, gorsel, barkod, aciklama) getirir -
    ayrica her urun sayfasini tek tek ziyaret etmeye gerek kalmaz.
    """
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

    # 1. ONCE: sitemap.xml uzerinden tum urun URL'lerini bulmayi dene.
    #    Basarili olursa kategori tahmini/sayfalama sorunlariyla ugrasmaya gerek kalmaz.
    base_domain = "https://" + store["start_urls"][0].split("/")[2]
    product_urls = set(try_sitemap_urls(scraper, base_domain, stats))

    if product_urls:
        print(f"[{store['name']}] Sitemap uzerinden {len(product_urls)} urun linki bulundu (kategori taramasi atlaniyor)")
        # Paralel indirme sayesinde artik tek calistirmada cok daha fazla urun
        # islenebiliyor. Bu sadece asiri uc durumlar (sitemap'in kozmetik disi
        # binlerce urun icermesi gibi) icin bir güvenlik agi - normalde devreye girmez.
        MAX_PRODUCTS_PER_STORE = 20000
        if len(product_urls) > MAX_PRODUCTS_PER_STORE:
            sorted_urls = sorted(product_urls)
            total = len(sorted_urls)
            day_index = datetime.now(timezone.utc).timetuple().tm_yday
            start = (day_index * MAX_PRODUCTS_PER_STORE) % total
            end = start + MAX_PRODUCTS_PER_STORE
            if end <= total:
                chunk = sorted_urls[start:end]
            else:
                chunk = sorted_urls[start:] + sorted_urls[:end - total]
            nights_to_cover_all = -(-total // MAX_PRODUCTS_PER_STORE)
            print(f"[{store['name']}] UYARI: {total} link var, bu calistirmada {MAX_PRODUCTS_PER_STORE} tanesi islenecek. Tum katalog ~{nights_to_cover_all} calistirmada taranir.")
            product_urls = set(chunk)
    else:
        # 2. SITEMAP YOKSA/BOSSA: eski yontem - kategori sayfalarini gez
        print(f"[{store['name']}] Sitemap bulunamadi, kategori sayfalari taranacak")
        for cat_url in store["start_urls"]:
            urls = extract_product_urls_from_category(scraper, cat_url, stats, pagination_param, max_pages)
            print(f"[{store['name']}] {cat_url} -> {len(urls)} link bulundu")
            for u in urls:
                product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")

    idx = 0
    # PARALEL INDIRME: ayni anda CONCURRENT_WORKERS kadar urun sayfasi indirilir.
    # Indirme (network - yavas kisim) paralel yapilir; veritabani yazma islemleri
    # ise indirmeler tamamlandikca TEK TEK (sirayla) yapilir - boylece ayni marka/
    # malzemenin ayni anda 2 kez olusturulmasi gibi celismeler (race condition) onlenir.
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
                    # Her 50 uruncte bir ilerleme ozeti - workflow'un takilip takilmadigini
                    # (veya sadece yavas oldugunu) loglardan anlik takip edebilmek icin.
                    if idx % 50 == 0:
                        print(f"[{store['name']}] --- İLERLEME: {idx}/{len(product_urls)} işlendi ---")
            except Exception as e:
                print(f"[{store['name']}] Hata: {e}")
                stats["skipped"] += 1
                continue

    print(f"[{store['name']}] ÖZET -> Yeni: {stats['new_products']} | Fiyat güncellenen: {stats['updated_prices']} | Atlanan: {stats['skipped']} | Status kodları: {stats['status_codes']}")
    return stats

def main():
    import sys
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")

    # Komut satirindan bir magaza slug'i verilirse (orn: "python scraper.py gratis")
    # SADECE o magaza islenir. Bu, GitHub Actions'ta 8 magazayi paralel/ayri
    # islere bolmek icin kullanilir. Argument verilmezse (eskisi gibi) tum
    # magazalar sirayla islenir - geriye donuk uyumluluk icin.
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
            # Shopify magazalari icin ozel API tabanli akis kullanilir,
            # digerleri genel HTML kazima akisini kullanir.
            if store.get("platform") == "shopify":
                overall[store["name"]] = process_store_shopify(store)
            else:
                overall[store["name"]] = process_store(store)
            time.sleep(3)  # Mağazalar arası 3 saniye dinlenme (Ban koruması)
        except Exception as e:
            print(f"{store['name']} atlandı: {e}")
            continue

    print("\n==================== GENEL ÖZET ====================")
    for name, s in overall.items():
        print(f"{name}: Yeni={s['new_products']} FiyatGüncel={s['updated_prices']} Atlanan={s['skipped']} Status={s['status_codes']}")
    print("\nTaranma Tamamlandı!")

if __name__ == "__main__":
    main()
