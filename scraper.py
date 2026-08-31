import os
import re
import time
import random
import json
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

# 8 Mağaza ve Genişletilmiş Kategori/API Linkleri
RETAILERS = [
    {
        "name": "Gratis", 
        "slug": "gratis", 
        "start_urls": [
            "https://www.gratis.com/makyaj-c-100",
            "https://www.gratis.com/cilt-bakim-c-200",
            "https://www.gratis.com/sac-bakim-c-300",
            "https://www.gratis.com/parfum-deodorant-c-400"
        ]
    },
    {
        "name": "Watsons", 
        "slug": "watsons", 
        "start_urls": [
            "https://www.watsons.com.tr/cilt-bakim/c/1010",
            "https://www.watsons.com.tr/makyaj/c/1000",
            "https://www.watsons.com.tr/sac-bakim/c/1020",
            "https://www.watsons.com.tr/parfum/c/1040"
        ]
    },
    {
        "name": "Sephora", 
        "slug": "sephora", 
        "start_urls": [
            "https://www.sephora.com.tr/shop/makyaj-c302/",
            "https://www.sephora.com.tr/shop/cilt-bakim-c303/",
            "https://www.sephora.com.tr/shop/parfum-c301/"
        ]
    },
    {
        "name": "Sevil Parfümeri", 
        "slug": "sevil", 
        "start_urls": [
            "https://www.sevil.com.tr/parfum.html",
            "https://www.sevil.com.tr/makyaj.html",
            "https://www.sevil.com.tr/cilt-bakimi.html"
        ]
    },
    {
        "name": "Boyner Beauty", 
        "slug": "boyner", 
        "start_urls": [
            "https://www.boyner.com.tr/kozmetik-c-10",
            "https://www.boyner.com.tr/parfum-c-1001",
            "https://www.boyner.com.tr/cilt-bakim-c-1002"
        ]
    },
    {
        "name": "Rossmann", 
        "slug": "rossmann", 
        "start_urls": [
            "https://www.rossmann.com.tr/makyaj-c-101",
            "https://www.rossmann.com.tr/cilt-bakimi-c-102",
            "https://www.rossmann.com.tr/sac-bakimi-c-103"
        ]
    },
    {
        "name": "Eve Shop", 
        "slug": "eveshop", 
        "start_urls": [
            "https://www.eveshop.com.tr/makyaj",
            "https://www.eveshop.com.tr/cilt-bakimi",
            "https://www.eveshop.com.tr/parfum"
        ]
    },
    {
        "name": "Kozmela", 
        "slug": "kozmela", 
        "start_urls": [
            "https://www.kozmela.com/cilt-bakimi",
            "https://www.kozmela.com/makyaj",
            "https://www.kozmela.com/parfum"
        ]
    }
]

MAX_PRODUCTS_PER_STORE = None  # Tam tarama modu (Limit kaldırıldı)
REQUEST_DELAY = 1.0

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
    # Her kategorinin ilk 5 sayfasını derinlemesine tarar
    for page in range(1, 6):
        try:
            page_url = f"{cat_url}?page={page}" if "?" not in cat_url else f"{cat_url}&page={page}"
            res = scraper.get(page_url, timeout=15)
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
        except Exception as e:
            print(f"Kategori Hatasi ({cat_url} - Sayfa {page}): {e}")
            break
    return list(found_urls)

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
    print(f"[{store['name']}] Bulunan Toplam Urun Linki Sayisi: {len(product_urls)}")
    saved_count = 0

    for idx, url in enumerate(product_urls, start=1):
        try:
            p_res = scraper.get(url, timeout=12)
            if p_res.status_code != 200: continue
            soup = BeautifulSoup(p_res.text, "html.parser")

            h1 = soup.select_one("h1")
            if not h1: continue
            name = clean_text(h1.get_text())

            brand_el = soup.select_one("[class*='brand'], [itemprop='brand']")
            brand_name = clean_text(brand_el.get_text()) if brand_el else "Genel"
            brand_id = get_or_create_brand(brand_name)

            price = None
            price_el = soup.select_one("[class*='price'], [itemprop='price']")
            if price_el: price = clean_price(price_el.get_text())

            img_el = soup.select_one("meta[property='og:image']")
            image_url = img_el.get("content") if img_el else None

            category = "Kozmetik"
            lower_name = name.lower()
            if any(w in lower_name for w in ["krem", "nemlendirici", "serum", "tonik", "temizleyici"]): category = "Cilt Bakımı"
            elif any(w in lower_name for w in ["parfüm", "edt", "edp", "deodorant"]): category = "Parfüm"
            elif any(w in lower_name for w in ["şampuan", "saç kremi", "maske", "saç yağ"]): category = "Saç Bakımı"
            elif any(w in lower_name for w in ["ruj", "fondöten", "maskara", "allık", "kapatıcı"]): category = "Makyaj"

            inci_text = None
            for el in soup.find_all(["div", "p", "span"]):
                txt = el.get_text()
                if ("İçindekiler" in txt or "Ingredients" in txt) and len(txt) > 30 and "," in txt:
                    inci_text = clean_text(txt.replace("İçindekiler:", "").replace("Ingredients:", ""))
                    break

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
        except Exception:
            continue

def main():
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")
    for store in RETAILERS:
        try:
            process_store(store)
        except Exception:
            continue
    print("\nTüm Mağazaların Taranması Tamamlandı!")

if __name__ == "__main__":
    main()
