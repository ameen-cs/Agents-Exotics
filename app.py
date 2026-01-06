# app.py
from flask import Flask, render_template, request, jsonify
import requests
from dotenv import load_dotenv
import os
import json
import time
from datetime import datetime
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

app = Flask(__name__)

# Configuration
USERNAME = os.getenv("AUTOTRADER_USERNAME")
PASSWORD = os.getenv("AUTOTRADER_PASSWORD")
API_URL = os.getenv("API_URL")

# Cache settings
CACHE_FILE = "cache_listings.json"
CACHE_TIMEOUT = 1800  # 30 minutes (in seconds)


# Inject current time for footer copyright
@app.context_processor
def inject_now():
    return {'now': datetime.utcnow}


class VehicleListingProcessor:
    """Handles processing and formatting of vehicle listings"""

    @staticmethod
    def is_armoured(description, make="", model=""):
        """Detect if a vehicle is armoured based on description or known models"""
        if not description:
            description = ""
        description_lower = description.lower()
        make_model = f"{make} {model}".lower()
        armoured_keywords = [
            'armoured', 'armored', 'bulletproof', 'b6', 'b7', 'vr7', 'vr9',
            'runflat', 'reinforced', 'protection', 'executive protection'
        ]
        for keyword in armoured_keywords:
            if keyword in description_lower or keyword in make_model:
                return True
        return False

    @staticmethod
    def parse_iso_datetime(dt_str):
        try:
            if not dt_str:
                return 0
            if dt_str.endswith('Z'):
                dt_str = dt_str[:-1] + '+00:00'
            if '+' in dt_str and len(dt_str.split('+')[-1]) == 4:
                parts = dt_str.split('+')
                dt_str = f"{parts[0]}+{parts[1][:2]}:{parts[1][2:]}"
            dt = datetime.fromisoformat(dt_str)
            return dt.timestamp()
        except Exception as e:
            logger.warning(f"Failed to parse datetime: {dt_str} | Error: {e}")
            return 0

    @staticmethod
    def format_price(raw_price_str):
        price_display = "POA"
        price_value_for_sorting = 0

        if not raw_price_str or (isinstance(raw_price_str, str) and 
                                raw_price_str.upper() in ["POA", "ON REQUEST", "PRICE ON APPLICATION", ""]):
            return price_display, price_value_for_sorting

        try:
            if isinstance(raw_price_str, str):
                raw_price_str = raw_price_str.strip()
                parts = raw_price_str.split(',')
                if len(parts) == 2:
                    major_part_str = parts[0]
                    minor_part_str = parts[1][:2]
                    major_digits_only = ''.join(filter(str.isdigit, major_part_str))
                    if not major_digits_only:
                        major_digits_only = "0"
                    price_float_str = f"{major_digits_only}.{minor_part_str}"
                    price_value_for_sorting = float(price_float_str)
                    major_int = int(major_digits_only)
                    formatted_major = f"{major_int:,}".replace(',', ' ')
                    price_display = f"R{formatted_major}"
                else:
                    clean_str = ''.join(filter(str.isdigit, raw_price_str))
                    if clean_str:
                        price_value_for_sorting = float(clean_str)
                        price_display = f"R{price_value_for_sorting:,.0f}".replace(',', ' ')
            elif isinstance(raw_price_str, (int, float)):
                price_value_for_sorting = float(raw_price_str)
                price_display = f"R{price_value_for_sorting:,.0f}".replace(',', ' ')
        except (ValueError, IndexError, TypeError) as e:
            logger.warning(f"Error parsing price '{raw_price_str}': {e}")
        return price_display, price_value_for_sorting

    @staticmethod
    def format_mileage(mileage):
        try:
            mileage_int = int(mileage) if mileage else 0
            return f"{mileage_int:,}".replace(',', ' ')
        except (ValueError, TypeError):
            return "0"

    @staticmethod
    def process_listing(item):
        make = item.get("make", "Unknown").title()
        model = item.get("model", "Model").title()
        year = item.get("year", "N/A")
        location = item.get("location", "South Africa")
        colour = item.get("colour", "Unknown")
        description = item.get("description", "No description available.").replace('\r', '')
        variant = item.get("variant", "")
        body_type = item.get("bodyType", "")
        engine = item.get("engine", "N/A")

        price_display, price_value_for_sorting = VehicleListingProcessor.format_price(item.get("price", ""))
        formatted_mileage = VehicleListingProcessor.format_mileage(item.get("mileageInKm", 0))
        image_urls = item.get("imageUrls", [])
        if not image_urls:
            image_urls = [f"https://source.unsplash.com/random/800x600/?car,{make.lower()}+{model.lower()}"]
        created = item.get("created", "")
        created_timestamp = VehicleListingProcessor.parse_iso_datetime(created) if created else time.time()

        # Detect armoured status
        is_armoured = VehicleListingProcessor.is_armoured(description, make, model)

        return {
            "id": item.get("id"),
            "make": make,
            "model": model,
            "year": year,
            "price_display": price_display,
            "price": price_value_for_sorting,
            "image_urls": image_urls,
            "variant": variant,
            "body_type": body_type,
            "colour": colour,
            "location": location,
            "mileage": formatted_mileage,
            "description": description,
            "created": created,
            "created_timestamp": created_timestamp,
            "engine": engine,
            "is_armoured": is_armoured  # ✅ Added
        }


class CacheManager:
    @staticmethod
    def get_listings_from_cache():
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    cache = json.load(f)
                if time.time() - cache["timestamp"] < CACHE_TIMEOUT:
                    logger.info("✅ Using cached API data")
                    return cache["data"]
                else:
                    logger.info("⏳ Cache expired, will fetch fresh data")
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning(f"⚠️ Cache file corrupted or invalid: {e}")
        return None

    @staticmethod
    def save_listings_to_cache(data):
        try:
            cache = {
                "timestamp": time.time(),
                "data": data
            }
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2, ensure_ascii=False)
            logger.info("💾 Fresh data saved to cache")
        except Exception as e:
            logger.error(f"❌ Failed to save cache: {e}")


class APIClient:
    """Handles API communication"""
    
    @staticmethod  # ✅ Properly indented inside class
    def fetch_listings_from_api():
        logger.info("📡 Fetching fresh data from API...")
        try:
            response = requests.get(
                API_URL,
                auth=(USERNAME, PASSWORD),
                timeout=10,
                headers={"Accept": "application/json"}
            )

            logger.info(f"API Response Status: {response.status_code}")
            
            if response.status_code != 200:
                logger.error(f"API Error Body: {response.text[:300]}")
                return None

            raw_data = response.json()
            if isinstance(raw_data, list):
                return raw_data
            elif isinstance(raw_data, dict):
                return raw_data.get("listings", []) or raw_data.get("vehicles", []) or []
            else:
                return []
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error: {e}")
            return None
        except ValueError as e:
            logger.error(f"Invalid JSON from API: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return None


def fetch_listings():
    cached_data = CacheManager.get_listings_from_cache()
    
    if cached_data is not None:
        raw_listings = cached_data
    else:
        raw_listings = APIClient.fetch_listings_from_api()
        if raw_listings is not None:
            CacheManager.save_listings_to_cache(raw_listings)
        elif cached_data is not None:
            logger.warning("⚠️ Using stale cache due to API failure")
            raw_listings = cached_data
        else:
            logger.error("❌ No cache available. Showing empty list.")
            raw_listings = []

    listings = [VehicleListingProcessor.process_listing(item) for item in raw_listings]
    listings.sort(key=lambda x: x["created_timestamp"], reverse=True)
    logger.info(f"📦 Total processed & sorted listings: {len(listings)}")
    return listings


# ——— ROUTES ———

@app.route("/")
def home():
    try:
        listings = fetch_listings()
        sorted_by_price = sorted(listings, key=lambda x: x.get("price", 0), reverse=True)
        featured_listings = sorted_by_price[:3] if sorted_by_price else []
        return render_template("home.html", featured_listings=featured_listings)
    except Exception as e:
        logger.error(f"Error in home route: {e}")
        return render_template("home.html", featured_listings=[])


@app.route("/services")
def services():
    return render_template("services.html")


@app.route("/about")
def about():
    return render_template("about.html")


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route("/inventory")
def inventory():
    try:
        listings = fetch_listings()
        sort = request.args.get('sort', 'newest')
        armoured = request.args.get('armoured', 'all')

        if armoured == 'yes':
            listings = [car for car in listings if car.get('is_armoured')]
        elif armoured == 'no':
            listings = [car for car in listings if not car.get('is_armoured')]

        if sort == 'price_high':
            listings.sort(key=lambda x: x.get('price', 0), reverse=True)
        elif sort == 'price_low':
            listings.sort(key=lambda x: x.get('price', 0))

        return render_template("index.html", listings=listings, sort=sort, armoured=armoured)
    except Exception as e:
        logger.error(f"Error in inventory route: {e}")
        return render_template("index.html", listings=[], sort='newest', armoured='all')


@app.route("/listing/<listing_id>")
def listing_detail(listing_id):
    try:
        listings = fetch_listings()
        for listing in listings:
            if str(listing.get("id")) == str(listing_id):
                return render_template("listing.html", car=listing)
        # If not found, return 404 (no secondary API call needed)
        return "Vehicle not found", 404
    except Exception as e:
        logger.error(f"Error fetching listing {listing_id}: {e}")
        return "Vehicle not found", 404


# Legacy redirects
@app.route("/about.html")
def about_legacy():
    return render_template("about.html")

@app.route("/contact.html")
def contact_legacy():
    return render_template("contact.html")

@app.route("/finance")
@app.route("/finance.html")
def finance():
    return render_template("finance.html")

@app.route("/trade-in")
def trade_in():
    return render_template("trade-in.html")

@app.route("/gallery")
def gallery():
    return render_template("gallery.html")

@app.route("/privacy-policy")
def privacy_policy():
    return render_template("privacy-policy.html")


@app.route("/health")
def health_check():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)