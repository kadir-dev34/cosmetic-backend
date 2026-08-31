import os
import re
import time
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

# 8 Mağaza Yapılandırması
RETAILERS = [
    {"name": "Gratis", "slug": "gratis", "sitemap": "https://www.gratis.com/sitemap.xml"},
    {"name": "Watsons", "slug": "watsons", "sitemap": "https://www.watsons.com.tr/sitemap.xml"},
    {"name": "Sephora", "slug": "sephora", "sitemap": "https://www.sephora.com.tr/sitemap.xml"},
    {"name": "Sevil Parfümeri", "slug": "sevil", "sitemap": "https://www.sevil.com.tr/sitemap.xml"},
    {"name": "Boyner Beauty", "slug": "boyner", "sitemap": "https://www.boyner.com.tr/sitemap.xml"},
    {"name": "Rossmann", "slug": "rossmann", "sitemap": "https://www.rossmann.com.tr/sitemap.xml"},
    {"name": "Eve Shop", "slug": "eveshop", "sitemap": "https://www.eveshop.com.tr/sitemap.xml"},
    {"name": "Kozmela", "slug": "kozmela", "sitemap": "https://www.kozmela.com/sitemap.xml"}
]

# Her mağazadan ilk etapta hızlıca 50'şer ürün çekerek 8 mağazanın da çalıştığını doğrulayalım
MAX_PRODUCTS_PER_STORE = 50 
REQUEST_DELAY = 0.5  # İstek arası bekleme 0.5 sn'ye indirildi (Hızlandırma)

scraper = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "windows", "mobile": False}
)
scraper.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7"
})

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
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")

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

def create_scrape_log(retailer_id):
    possible_statuses = ["in_progress", "pending", "started", "running", "RUNNING", "IN_PROGRESS"]
    for status_val in possible_statuses:
        try:
            res = supabase.table("scrape_logs").insert({
                "retailer_id": retailer_id,
                "status": status_val
            }).execute()
            if res.data: return res.data[0]["id"]
        except Exception:
            continue
    return None

def update_scrape_log(log_id, status_type, found_count=0, saved_count=0, error_msg=None):
    if not log_id: return
    status_map = {
        "success": ["success", "completed", "SUCCESS", "COMPLETED", "ok"],
        "failed": ["failed", "error", "FAILED", "ERROR"]
    }
    for status_val in status_map.get(status_type, [status_type]):
        try:
            payload = {
                "status": status_val,
                "finished_at": datetime.now(timezone.utc).isoformat()
            }
            if status_type == "success":
                payload["products_found"] = found_count
                payload["products_saved"] = saved_count
            else:
                payload["error_message"] = str(error_msg)[:255] if error_msg else None
            supabase.table("scrape_logs").update(payload).eq("id", log_id).execute()
            break
        except Exception:
            continue

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
        last_price = supabase.table("product_prices")\
            .select("price")\
            .eq("product_id", product_id)\
            .eq("retailer_id", retailer_id)\
            .order("recorded_at", desc=True)\
            .limit(1)\
            .execute()
            
        if last_price.data and float(last_price.data[0]["price"]) == float(price):
            return

        supabase.table("product_prices").insert({
            "product_id": product_id,
            "retailer_id": retailer_id,
            "price": price,
            "currency": "TRY",
            "product_url": product_url,
            "is_available": True
        }).execute()
    except Exception as e:
        print(f"Fiyat kayit hatasi: {e}")

def process_store(store):
    print(f"\n==================== {store['name']} Taranıyor ====================")
    retailer_id = get_or_create_retailer(store["name"], store["slug"])
    log_id = create_scrape_log(retailer_id)

    try:
        res = scraper.get(store["sitemap"], timeout=15)
        urls = list(set(re.findall(r"<loc>(https?://[^<]+)</loc>", res.text)))
        product_urls = [u for u in urls if "-p-" in u or "/p/" in u or "urun" in u or "product" in u]
        
        if not product_urls:
            print(f"{store['name']} sitemap'te doğrudan ürün linki bulunamadı, ana sayfa deneniyor...")
            product_urls = urls[:MAX_PRODUCTS_PER_STORE]

        if MAX_PRODUCTS_PER_STORE:
            product_urls = product_urls[:MAX_PRODUCTS_PER_STORE]

        print(f"[{store['name']}] Taranacak Ürün Sayısı: {len(product_urls)}")
        saved_count = 0
        
        for idx, url in enumerate(product_urls, start=1):
            try:
                p_res = scraper.get(url, timeout=10)
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
                existing = supabase.table("products").select("id").eq("slug", slug).limit(1).execute()
                
                if existing.data:
                    product_id = existing.data[0]["id"]
                else:
                    ins = supabase.table("products").insert({
                        "brand_id": brand_id,
                        "name": name,
                        "slug": slug,
                        "category": category,
                        "barcode": barcode,
                        "image_url": image_url,
                        "original_inci_text": inci_text
                    }).execute()
                    product_id = ins.data[0]["id"]

                save_ingredients(product_id, inci_text)
                save_product_image(product_id, image_url)
                save_price(product_id, retailer_id, price, url)

                saved_count += 1
                print(f"[{store['name']}] [{idx}/{len(product_urls)}] İşlendi: {name[:25]}... | {price} TL")
                time.sleep(REQUEST_DELAY)
            except Exception as e:
                continue

        update_scrape_log(log_id, "success", found_count=len(product_urls), saved_count=saved_count)

    except Exception as e:
        print(f"Mağaza Hatası ({store['name']}): {e}")
        update_scrape_log(log_id, "failed", error_msg=e)

def main():
    print("Gece Otomatik Kozmetik Scraper Başlatıldı...")
    for store in RETAILERS:
        try:
            process_store(store)
        except Exception as e:
            print(f"{store['name']} taranırken genel hata atlandı: {e}")
            continue
    print("\nTüm Mağazaların Taranması Tamamlandı!")

if __name__ == "__main__":
    main()
