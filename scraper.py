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

RETAILERS = [
    {
        "name": "Gratis", 
        "slug": "gratis", 
        "start_urls": ["https://www.gratis.com/makyaj-c-100", "https://www.gratis.com/cilt-bakim-c-200"]
    },
    {
        "name": "Watsons", 
        "slug": "watsons", 
        "start_urls": ["https://www.watsons.com.tr/cilt-bakim/c/1010", "https://www.watsons.com.tr/makyaj/c/1000"]
    },
    {
        "name": "Sephora", 
        "slug": "sephora", 
        "start_urls": ["https://www.sephora.com.tr/shop/makyaj-c302/", "https://www.sephora.com.tr/shop/cilt-bakim-c303/"]
    },
    {
        "name": "Sevil Parfümeri", 
        "slug": "sevil", 
        "start_urls": ["https://www.sevil.com.tr/parfum.html", "https://www.sevil.com.tr/cilt-bakimi.html"]
    },
    {
        "name": "Boyner Beauty", 
        "slug": "boyner", 
        "start_urls": ["https://www.boyner.com.tr/kozmetik-c-10", "https://www.boyner.com.tr/parfum-c-1001"]
    },
    {
        "name": "Rossmann", 
        "slug": "rossmann", 
        "start_urls": ["https://www.rossmann.com.tr/makyaj-c-101", "https://www.rossmann.com.tr/cilt-bakimi-c-102"]
    },
    {
        "name": "Eve Shop", 
        "slug": "eveshop", 
        "start_urls": ["https://www.eveshop.com.tr/makyaj", "https://www.eveshop.com.tr/cilt-bakimi"]
    },
    {
        "name": "Kozmela", 
        "slug": "kozmela", 
        "start_urls": ["https://www.kozmela.com/cilt-bakimi", "https://www.kozmela.com/makyaj"]
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

def extract_product_urls_from_category(scraper, cat_url):
    found_urls = set()
    for page in range(1, 4):
        try:
            page_url = f"{cat_url}?page={page}" if "?" not in cat_url else f"{cat_url}&page={page}"
            res = scraper.get(page_url, timeout=12)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if any(k in href for k in ["-p-", "/p/", "urun", "product", ".html", "-pr-", "/pr/"]) and not href.startswith("javascript"):
                        if href.startswith("/"):
                            base = "/".join(cat_url.split("/")[:3])
                            href = base + href
                        found_urls.add(href)
                
                script_urls = re.findall(r'https?://[^\s"\'<>]+(?:-p-|-pr-|/p/|/pr/|urun)[^\s"\'<>]*', res.text)
                for u in script_urls:
                    found_urls.add(u)
            time.sleep(0.5)
        except Exception:
            break
    return list(found_urls)

def parse_product_page(soup, p_res):
    """Mağazaya özel esnek ürün adı, marka ve fiyat çıkarma."""
    # Ürün Adı
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
        # İsmin ilk kelimesinden marka çıkarma tahmini
        parts = name.split()
        if len(parts) > 1: brand_name = parts[0]

    # Fiyat
    price = None
    price_el = soup.select_one("[class*='price'], [itemprop='price'], .current-price, .discount-price, span[data-price]")
    if price_el:
        price = clean_price(price_el.get_text())

    if not price:
        # HTML içindeki TL kalıplarını arama
        matches = re.findall(r'(\d+[\.,]?\d*)\s*(?:TL|₺)', p_res.text)
        if matches:
            for m in matches:
                p = clean_price(m)
                if p and p > 5:
                    price = p
                    break

    # Görsel
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

    product_urls = set()
    for cat_url in store["start_urls"]:
        urls = extract_product_urls_from_category(scraper, cat_url)
        for u in urls:
            product_urls.add(u)

    product_urls = list(product_urls)
    print(f"[{store['name']}] Bulunan Urun Linki Sayisi: {len(product_urls)}")
    saved_count = 0

    for idx, url in enumerate(product_urls, start=1):
        try:
            p_res = scraper.get(url, timeout=12)
            if p_res.status_code != 200: continue
            soup = BeautifulSoup(p_res.text, "html.parser")

            name, brand_name, price, image_url, inci_text = parse_product_page(soup, p_res)
            if not name: continue

            brand_id = get_or_create_brand(brand_name)

            category = "Kozmetik"
            lower_name = name.lower()
            if any(w in lower_name for w in ["krem", "nemlendirici", "serum", "tonik", "temizleyici"]): category = "Cilt Bakımı"
            elif any(w in lower_name for w in ["parfüm", "edt", "edp", "deodorant"]): category = "Parfüm"
            elif any(w in lower_name for w in ["şampuan", "saç kremi", "maske", "saç yağ"]): category = "Saç Bakımı"
            elif any(w in lower_name for w in ["ruj", "fondöten", "maskara", "allık", "kapatıcı"]): category = "Makyaj"

            barcode = None
            bc_match = re.search(r"\b\d{13}\b", p_res.text)
            if bc_match: barcode = bc_match.group(0)

            slug = make_slug(name)
            unique_slug = f"{slug}-{store['slug']}"
            existing = supabase.table("products").select("id").eq("slug", unique_slug).limit(1).execute()

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
                save_ingredients(product_id, inci_text)
                save_product_image(product_id, image_url)
                save_price(product_id, retailer_id, price, url)
                saved_count += 1
                print(f"[{store['name']}] [{idx}/{len(product_urls)}] Eklendi: {name[:20]}... | {price} TL")

            time.sleep(REQUEST_DELAY)
        except Exception as e:
            print(f"[{store['name']}] Hata: {e}")
            continue

def main():
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")
    for store in RETAILERS:
        try:
            process_store(store)
            time.sleep(3)  # Mağazalar arası 3 saniye dinlenme (Ban koruması)
        except Exception as e:
            print(f"{store['name']} atlandı: {e}")
            continue
    print("\nTüm Mağazaların Taranması Tamamlandı!")

if __name__ == "__main__":
    main()
