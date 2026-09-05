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

# Log Ayarları
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

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
        "max_pages": 15,
        # BUG FIX #6 (Gratis kapsam sorunu): 13.996 link icin worker=1 + 1-2.5s
        # bekleme ile 45 dk'da sadece ~1.300 urun islenebiliyordu (Zaman Asimi:
        # True, katalogun ~%90'i hic taranmadan atlaniyordu). Gratis, Sephora
        # gibi agresif bot korumasi olan bir site DEGIL (cloudscraper sorunsuz
        # calisiyor), bu yuzden burada worker sayisini guvenle artirabiliriz.
        "concurrent_workers": 4
    },
    {
        "name": "Sephora",
        "slug": "sephora",
        "start_urls": ["https://www.sephora.com.tr/makyaj-c302/", "https://www.sephora.com.tr/cilt-bakim-c303/"],
        "pagination_param": "page",
        "max_pages": 15,
        # Sephora korumali bir site (tum urun sayfalarinda 403 aliniyor) -
        # worker sayisini artirmak sorunu cozmez, sadece 403 sayisini artirir.
        "concurrent_workers": 1
    },
    {
        "name": "Boyner Beauty",
        "slug": "boyner",
        "start_urls": ["https://www.boyner.com.tr/kozmetik-c-10", "https://www.boyner.com.tr/parfum-c-1001"],
        "pagination_param": "page",
        "max_pages": 15,
        "concurrent_workers": 2
    },
    {
        "name": "Kozmela",
        "slug": "kozmela",
        "start_urls": ["https://www.kozmela.com/cilt-bakimi", "https://www.kozmela.com/makyaj"],
        "pagination_param": "page",
        "max_pages": 15,
        "concurrent_workers": 2
    }
]

# Varsayilan worker sayisi (store bazinda "concurrent_workers" verilmezse kullanilir)
CONCURRENT_WORKERS = 1

# Bir mağazanın taranması bu süreyi (saniye) aşarsa, kalan ürünler atlanıp
# elde edilen sonuçlarla devam edilir. Bu, bir mağazadaki hata/yavaşlığın
# (örn. eski Gratis 3 saat sürme sorunu) diğer mağazaların hiç taranamamasına
# (Sephora'nın "cancelled" olmasına) yol açmasını engeller.
MAX_STORE_RUNTIME_SECONDS = int(os.getenv("MAX_STORE_RUNTIME_SECONDS", 60 * 40))  # varsayilan 40 dakika


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


# ------------------------------------------------------------------
# BUG (Sephora 403): Tum urun sayfalarina yapilan istekler 403 donuyordu
# (sitemap istekleri 200 donuyor, yani engel sadece urun sayfalarinda).
# Bunun kesin nedeni bu ortamdan (network erisimi kapali) dogrulanamaz,
# ama en olasi ihtimaller: (a) Referer header'i olmadan dogrudan sitemap'ten
# gelen URL'lere "atlanmis" gibi gorunmek, (b) ayni oturumun cok sayida
# istek sonrasi isaretlenmesi. Asagidaki degisiklikler ikisini de hedefler:
# base URL'i Referer olarak eklemek + 403 alinca oturumu tazeleyip (yeni
# TLS/cookie fingerprint ile) bir kez daha, daha uzun bir bekleme ile
# denemek. Bu, guclu bot korumalarini (Akamai/PerimeterX/Datadome tarzi)
# kesin cozmez -- cozmezse gercek ihtiyac headless bir tarayicidir
# (orn. Playwright) ve bunu ayrica not ediyoruz.
# ------------------------------------------------------------------
def fetch_product_page(url, referer=None, min_delay=1.0, max_delay=2.5):
    time.sleep(random.uniform(min_delay, max_delay))
    try:
        scraper = get_thread_scraper()
        headers = {"Referer": referer} if referer else {}
        res = scraper.get(url, timeout=15, headers=headers)

        if res.status_code == 403:
            # Bir kez, daha uzun bekleme + taze oturumla tekrar dene
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
    """Sephora'daki bitişik marka+ürün adı sorununu çözer (Örn: GLOW RECIPEWatermelon -> Watermelon)"""
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
        logging.error(f"Marka ekleme hatasi ({brand_name}): {e}")
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
            logging.error(f"INCI kayit hatasi (Product: {product_id}, Item: {item}): {e}")


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
        logging.error(f"Gorsel kayit hatasi (Product: {product_id}): {e}")


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
        logging.error(f"Fiyat kayit hatasi (Product: {product_id}): {e}")


def parse_sitemap_url(sub_url, product_url_pattern, stats):
    found = set()
    try:
        scraper = get_thread_scraper()
        sub_res = scraper.get(sub_url, timeout=12)
        stats["status_codes"][f"sitemap:{sub_res.status_code}"] = stats["status_codes"].get(f"sitemap:{sub_res.status_code}", 0) + 1
        if sub_res.status_code == 200:
            sub_locs = re.findall(r"<loc>([^<]+)</loc>", sub_res.text)
            for loc in sub_locs:
                if product_url_pattern.search(loc):
                    found.add(loc)
    except Exception:
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

            for loc in locs:
                if loc not in sub_sitemaps and product_url_pattern.search(loc):
                    found_urls.add(loc)

            relevant_sub = [s for s in sub_sitemaps if re.search(r"product|urun|category|kategori", s, re.IGNORECASE)] or sub_sitemaps[:15]

            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = [executor.submit(parse_sitemap_url, sub_url, product_url_pattern, stats) for sub_url in relevant_sub[:15]]
                for future in as_completed(futures):
                    found_urls.update(future.result())

            if found_urls: break
        except Exception:
            continue

    return list(found_urls)


# ------------------------------------------------------------------
# BUG FIX #3 & #4 (Boyner/Kozmela): Kategori/listeleme sayfası linkleri
# ürün linki gibi toplanıyordu ("...-modelleri-boyner", "...urunler-kozmela"
# gibi URL'ler "urun" alt string'ini içerdiği için ürün sanılıyordu).
# Bu blocklist, bilinen kategori/listeleme URL kalıplarını dışlar.
# ------------------------------------------------------------------
CATEGORY_URL_BLOCKLIST = re.compile(
    r"(kategori|modelleri|koleksiyon|filtre=|sirala=|/c-\d|-c-\d+(?:/|$))",
    re.IGNORECASE
)


def _looks_like_product_url(href):
    if CATEGORY_URL_BLOCKLIST.search(href):
        return False
    return any(k in href for k in ["-p-", "/p/", "urun", "product", ".html", "-pr-", "/pr/", "/collections/"])


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
                if href.startswith("javascript"):
                    continue
                if not _looks_like_product_url(href):
                    continue
                if href.startswith("/"):
                    base = "/".join(cat_url.split("/")[:3])
                    href = base + href
                found_urls.add(href)

            script_urls = re.findall(r'https?://[^\s"\'<>]+(?:-p-|-pr-|/p/|/pr/|urun)[^\s"\'<>]*', res.text)
            for u in script_urls:
                if _looks_like_product_url(u):
                    found_urls.add(u)

            new_count = len(found_urls) - before_count
            if new_count == 0:
                consecutive_empty += 1
                if consecutive_empty >= 2: break
            else:
                consecutive_empty = 0

            time.sleep(0.8)
        except Exception:
            break

    return list(found_urls)


# ------------------------------------------------------------------
# BUG FIX #2 (Kozmela "799 TL" sorunu): [class*='price'] gibi geniş
# CSS seçiciler, "799 TL üzeri ücretsiz kargo" gibi kargo/taksit
# banner'larını "fiyat" sanıp yakalıyordu. Bu fonksiyon, eşleşen
# elementin (ve yakın ebeveyninin) metninde kargo/taksit gibi
# yanıltıcı kelimeler varsa o elementi reddeder.
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# BUG FIX #2b (Kozmela "799 TL" hatasi devam ediyordu): Onceki
# _is_valid_price_context() blacklist'i sadece bilinen kargo/taksit
# banner'larini yakaliyordu. Ama loglar gosterdi ki (run #41) 220
# urunun ~153'u yine BIREBIR AYNI fiyatla (799.0 TL) kaydedildi --
# tirnak makasi, sampuan, fondoten, biberon gibi tamamen farkli
# urunler. Bu, sitede her sayfada ayni sekilde goruntulenen (ama
# blacklist kelimelerini icermeyen) baska bir sabit widget'in genis
# "[class*='price']" secicisiyle yakalanmasindan kaynaklaniyor olmali.
#
# Blacklist kelime tahmin etmek yerine, DAVRANISSAL bir anomali
# kontrolu ekliyoruz: ayni fiyat degeri, en genis/en son care secici
# ("[class*='price']") uzerinden ayni store calistirmasi icinde N'den
# fazla kez tekrar ederse, bu deger artik guvenilmez sayilir ve o
# urun icin fiyat bulunamadi kabul edilir (urun atlanir, DB'ye yanlis
# fiyat yazilmaz). Gercek urunlerde ayni fiyatin birkac kez tekrar
# etmesi normaldir (ayni fiyatli farkli renkler vb.) - esik bu yuzden
# yuksek tutuldu (10), asil amac 153/220 gibi kitlesel tekrari yakalamak.
# ------------------------------------------------------------------
BROAD_PRICE_REPEAT_THRESHOLD = 10
_broad_price_counts_lock = threading.Lock()


def _register_broad_price_and_check(price_counts, price):
    """price_counts: process_store'dan gelen, store'a ozel paylasimli dict.
    True donerse fiyat guvenilir (kullanilabilir), False donerse suspicious."""
    with _broad_price_counts_lock:
        count = price_counts.get(price, 0) + 1
        price_counts[price] = count
        return count <= BROAD_PRICE_REPEAT_THRESHOLD


# ------------------------------------------------------------------
# BUG FIX #1 (Gratis içerik/ingredient sorunu): Eski kod, "İçindekiler"
# veya "Ingredients" kelimesini içeren İLK elementi (find_all sırası
# gereği genelde en dıştaki büyük wrapper div) kabul edip metnini
# olduğu gibi INCI listesi olarak kaydediyordu. Bu da yorumları, iade
# politikasını, firma adresini vs. "ingredient" olarak DB'ye yazıyordu
# ve binlerce gereksiz satır/istek yüzünden scraper'ı ciddi yavaşlatıyordu.
#
# Yeni yaklaşım:
#  - Eşleşen tüm adaylar toplanır, en KISA (en spesifik) aday seçilir.
#  - Kara liste kelimeleri (iade, yorum, kargo, sipariş vb.) içeren
#    adaylar elenir.
#  - Bir INCI listesinin virgülle ayrılmış parçaları kısa olmalıdır;
#    ortalama parça uzunluğu ve kelime sayısı bir "paragraf" gibi
#    görünüyorsa aday reddedilir.
# ------------------------------------------------------------------
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


# ------------------------------------------------------------------
# BUG FIX #3 (Boyner/Kozmela "kategori sayfası = ürün" sorunu):
# KNOWN_NON_PRODUCT_NAMES yalnızca tam eşleşen birkaç sabit isme
# bakıyordu ("Bakım Ürünleri Modelleri" gibi türetilmiş isimler
# kaçıyordu). Şimdi "Modelleri/Ürünleri/Ürünler" gibi tipik
# kategori-sayfası son ekleri regex ile de yakalanıyor. Ayrıca
# JSON-LD'de "Product" şeması bulunamadıysa (yani isim H1/og:title
# fallback'inden geldiyse), sayfada gerçek bir "Sepete Ekle" butonu
# olup olmadığı da kontrol ediliyor.
# ------------------------------------------------------------------
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
    is_confirmed_product = False  # JSON-LD'de @type=Product bulunduysa True

    # 1. JSON-LD Parsing
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

    # 2. HTML Fallback
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

    # ------------------------------------------------------------------
    # BUG FIX #3b (Boyner asiri filtreleme): JSON-LD ile dogrulanmayan
    # sayfalarda "gercek urun mu" testi olarak SADECE "Sepete Ekle"
    # buton/metni araniyordu, bulunamazsa sayfa TAMAMEN reddediliyordu.
    # Run #41'de Boyner'da 528 linkten 527'si bu yuzden atlandi -- Boyner'in
    # gercek HTML yapisi bu iki secicinin hicbirine uymuyor (buton JS ile
    # sonradan render ediliyor olabilir, farkli bir class ismi kullaniyor
    # olabilir vb.). Bu kontrolu SERTCE reddetmek yerine bir "guven puani"
    # sinyaline indirgiyoruz: cart isareti yoksa direkt eleriz ama SADECE
    # asagidaki fiyat kontrolunde de gecerli bir fiyat bulunamazsa (zaten
    # process_store'da "price is None -> atla" kontrolu var). Boylece
    # kategori sayfalari (fiyatsiz) yine elenir, ama fiyati olan gercek
    # urunler artik sirf "sepete ekle" metni bulunamadi diye kaybedilmez.
    # ------------------------------------------------------------------
    has_weak_cart_signal = True
    if not is_confirmed_product:
        has_cart_text = soup.find(string=re.compile(r"sepete ekle|add to cart|satın al", re.IGNORECASE))
        has_cart_button = soup.select_one("button[class*='cart'], button[class*='sepet'], [class*='add-to-cart']")
        has_weak_cart_signal = bool(has_cart_text or has_cart_button)
        # Ne cart isareti ne de JSON-LD var VE isim de zayifsa (tek/iki
        # kelime, buyuk ihtimalle bir menu/breadcrumb basligi), o zaman
        # dogrudan ele -- bu hala eski "kategori sayfasi" hatasina karsi
        # bir güvenlik agi, ama artik tek basina yeterli bir red sebebi degil.
        if not has_weak_cart_signal and word_count <= 3:
            return None, None, None, None, None

    if not brand_name or brand_name == "Genel":
        brand_el = soup.select_one("[class*='brand'], [itemprop='brand'], .product-brand")
        if brand_el:
            brand_name = clean_text(brand_el.get_text())
        else:
            parts = name.split()
            if len(parts) > 1: brand_name = parts[0]

    # Sephora bitişik isim düzeltmesini uygula
    name = fix_sephora_title(brand_name, name)

    if not price:
        # "[class*='price']" en genis/son care secici -- bir onceki Kozmela
        # "799 TL" hatasinin kaynagi buydu. Buna ozel anomali kontrolu uygulanir.
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
                        # Bu fiyat, ayni calistirmada esik degerinden fazla
                        # tekrar etti -> muhtemelen sabit bir widget/banner,
                        # gercek urun fiyati degil. Bu adayi reddet, sonraki
                        # elemana/seciciye gec.
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
            urls = extract_product_urls_from_category(scraper, cat_url, stats, pagination_param, max_pages)
            for u in urls: product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")

    idx = 0
    start_time = time.time()
    price_counts = {}  # BUG FIX #2b: Kozmela "799 TL" anomali kontrolu icin store'a ozel sayac
    workers = store.get("concurrent_workers", CONCURRENT_WORKERS)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_url = {
            executor.submit(fetch_product_page, url, referer=base_domain): url for url in product_urls
        }

        for future in as_completed(future_to_url):
            # BUG FIX #5 (Sephora hiç taranamadan iptal olması): Bir mağaza
            # çok uzun sürerse (eskiden Gratis ~3 saat sürüp job'ı tükettiği
            # için Sephora hiç başlayamadan cancel oldu), zaman sınırına
            # ulaşınca kalan ürünler atlanıp mevcut sonuçlarla devam edilir.
            # Not: Bu script-seviyesinde bir güvenlik önlemidir; asıl önerilen
            # çözüm, GitHub Actions workflow'unda mağazaları paralel matrix
            # job olarak, her birine ayrı timeout-minutes vererek çalıştırmaktır.
            if time.time() - start_time > MAX_STORE_RUNTIME_SECONDS:
                logging.warning(
                    f"[{store['name']}] Zaman siniri ({MAX_STORE_RUNTIME_SECONDS}s) asildi. "
                    f"Kalan urunler atlanip mevcut sonuclarla devam ediliyor."
                )
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
                existing = supabase.table("products").select("id, name").eq("slug", unique_slug).limit(1).execute()

                # BUG FIX #4 (slug çakışması): Aynı slug'a sahip ama adı
                # tamamen farklı bir ürün varsa (yanlışlıkla iki farklı
                # sayfa aynı slug'ı üretmiş olabilir), üzerine yazmak yerine
                # URL'e dayalı benzersiz bir slug ile yeni kayıt oluştur.
                if existing.data:
                    existing_name = (existing.data[0].get("name") or "").strip().lower()
                    if existing_name and existing_name != name.strip().lower():
                        url_hash = hashlib.md5(url.encode()).hexdigest()[:6]
                        unique_slug = f"{slug}-{url_hash}-{store['slug']}"
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
                    tag = "YENİ" if is_new_product else "GÜNCEL"
                    print(f"[{store['name']}] [{idx}/{len(product_urls)}] {tag}: {name[:30]}... | {price} TL")

            except Exception as e:
                print(f"[{store['name']}] Hata: {e}")
                stats["skipped"] += 1
                continue

    print(f"[{store['name']}] ÖZET -> Yeni: {stats['new_products']} | Fiyat güncellenen: {stats['updated_prices']} | Atlanan: {stats['skipped']} | Zaman Asimi: {stats['timed_out']} | Status kodları: {stats['status_codes']}")
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
            overall[store["name"]] = process_store(store)
            time.sleep(2)
        except Exception as e:
            print(f"{store['name']} atlandı: {e}")
            continue

    print("\n==================== GENEL ÖZET ====================")
    for name, s in overall.items():
        print(f"{name}: Yeni={s['new_products']} FiyatGüncel={s['updated_prices']} Atlanan={s['skipped']} ZamanAsimi={s.get('timed_out')} Status={s['status_codes']}")
    print("\nTaranma Tamamlandı!")


if __name__ == "__main__":
    main()
