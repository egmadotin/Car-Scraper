import os
import json
import time
import random
import logging
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()

# Configuration
USERNAME = os.getenv("PRODEMAND_USERNAME")
PASSWORD = os.getenv("PRODEMAND_PASSWORD")
MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/")
DB_NAME = os.getenv("DATABASE_NAME", "prodemand_selector")

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class QualifierBuffer:
    def __init__(self):
        self.responses = {}

    def capture(self, response):
        url = response.url
        if "api/Qualifier/GetQualifiers" in url:
            try:
                # Check if JSON
                if "application/json" in response.headers.get("content-type", ""):
                    data = response.json()
                    parsed_url = urlparse(url)
                    params = parse_qs(parsed_url.query)
                    # Normalize params for lookup (keys to lower, values are lists)
                    norm_params = {k.lower(): v[0] for k, v in params.items()}
                    
                    # Create a unique key based on relevant params
                    key_parts = []
                    for k in sorted(norm_params.keys()):
                        key_parts.append(f"{k}={norm_params[k]}")
                    key = "&".join(key_parts)
                    
                    self.responses[key] = data
                    # logger.info(f"  [Captured] {key}")
            except Exception as e:
                pass

    def get(self, **kwargs):
        # Normalize search params
        search_params = {k.lower(): str(v) for k, v in kwargs.items()}
        key_parts = []
        for k in sorted(search_params.keys()):
            key_parts.append(f"{k}={search_params[k]}")
        key = "&".join(key_parts)
        
        return self.responses.get(key)

    def clear(self):
        self.responses = {}

class VehicleScraper:
    def __init__(self, headless=False, target_year=None, force=False):
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=headless)
        self.context = self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        self.page = self.context.new_page()
        
        self.target_year = str(target_year) if target_year else None
        self.force = force
        self.buffer = QualifierBuffer()
        self.page.on("response", self.buffer.capture)
        
        self.mongo = MongoClient(MONGO_URI)
        self.db = self.mongo[DB_NAME]
        
        # Ensure unique index
        self.db.vehicles.create_index(
            [("year", 1), ("make", 1), ("model", 1), ("engine", 1), ("submodel", 1)],
            unique=True
        )

    def random_delay(self, min_s=1.5, max_s=3.0):
        time.sleep(random.uniform(min_s, max_s))

    def login(self):
        logger.info("Authenticating...")
        for attempt in range(3):
            try:
                self.page.goto("https://www1.prodemand.com/Authenticate/LoginRequired", wait_until="networkidle", timeout=60000)
                break
            except Exception as e:
                logger.warning(f"  Login goto attempt {attempt+1} failed: {e}")
                if attempt == 2: raise
                time.sleep(5)
        
        if self.page.is_visible("button:has-text('Accept All Cookies')"):
            self.page.click("button:has-text('Accept All Cookies')")
            time.sleep(2)

        if not self.page.is_visible("#username") and self.page.is_visible("text='Login'"):
            logger.info("Clicking Login on landing page...")
            self.page.locator("text='Login'").first.click()
            self.page.wait_for_selector("#username", timeout=30000)
            
        if self.page.is_visible("#username"):
            self.page.fill("#username", USERNAME)
            self.page.fill("#password", PASSWORD)
            self.page.click("#loginButton")
            
            self.page.wait_for_selector("#vehicleSelectionButton, #commitButton, #qualifierValueSelector", timeout=60000)
            
            if self.page.is_visible("#commitButton") or self.page.is_visible(".slick-row") or self.page.is_visible("text='Active Sessions'"):
                logger.info("Handling Session Manager...")
                try:
                    # Try clicking the first row directly
                    row = self.page.locator(".slick-row, .session-row").first
                    if row.is_visible():
                        box = row.bounding_box()
                        if box:
                            self.page.mouse.click(box['x'] + box['width']/2, box['y'] + box['height']/2)
                        else:
                            row.click(force=True)
                        time.sleep(2)
                    
                    if self.page.is_visible("#commitButton"):
                        self.page.click("#commitButton", force=True)
                        logger.info("  Commit clicked.")
                        time.sleep(5)
                        # Wait for the dashboard to load
                        self.page.wait_for_selector("#vehicleSelectionButton, #qualifierValueSelector", timeout=60000)
                except Exception as e_sm:
                    logger.warning(f"  Session manager handling failed: {e_sm}")
            
            logger.info("Login successful.")

    def select_qualifier(self, type_name, value, next_pattern=None):
        logger.info(f"  Selecting {type_name}: {value}")
        
        try:
            # 1. Click the type tab on the left
            tab_selector = f"#qualifierTypeSelector li:has-text('{type_name}')"
            if self.page.is_visible(tab_selector):
                self.page.click(tab_selector, force=True)
                self.random_delay(0.5, 1.0)

            # 2. Find the item
            # Special handling for _NA_ which might be the only item
            if value == "_NA_":
                item = self.page.locator("#qualifierValueSelector li").first
            else:
                item = self.page.locator(f"#qualifierValueSelector li:has-text('{value}')").first
            
            # Check if it's already selected/active
            if item.is_visible() and "active" in (item.get_attribute("class") or ""):
                logger.info(f"    {value} already selected.")
                return True

            # Scroll and wait
            try:
                item.scroll_into_view_if_needed(timeout=5000)
            except: pass
            
            item.wait_for(state="visible", timeout=10000)
            
            # Click and wait for response if pattern provided
            if next_pattern:
                try:
                    with self.page.expect_response(lambda res: next_pattern in res.url and "GetQualifiers" in res.url, timeout=10000):
                        item.click(force=True)
                except:
                    item.click(force=True)
            else:
                item.click(force=True)
                
            self.random_delay(2.0, 3.0)
            return True
        except Exception as e:
            logger.warning(f"    Selection failed for {type_name}={value}: {e}")
            # Fallback for Submodel
            if type_name == "Submodel":
                return True
            return False

    def run(self):
        try:
            self.login()
            
            # Start Selector
            if not self.page.is_visible("#qualifierValueSelector"):
                selectors = ["#vehicleSelectionButton", ".vehicleSelectionButton", "text='Select Vehicle'"]
                found = False
                for sel in selectors:
                    try:
                        if self.page.is_visible(sel):
                            self.page.click(sel)
                            found = True
                            break
                    except: pass
                
                if not found:
                    try: self.page.click("#vehicleSelectionButton")
                    except: pass
            
            self.page.wait_for_selector("#qualifierValueSelector", timeout=30000)
            
            # Phase 1: Years
            self.page.click("#qualifierTypeSelector li:has-text('Year')")
            self.random_delay()
            years_data = self.buffer.get(type="year")
            if not years_data:
                # Force refresh if not captured
                self.page.click("#qualifierTypeSelector li:has-text('Year')", force=True)
                self.random_delay(3, 5)
                years_data = self.buffer.get(type="year")
            
            if not years_data:
                raise Exception("Failed to capture Years API response.")

            try:
                years = [y.get('value', y.get('Value')) for y in years_data]
            except:
                logger.error(f"Years data structure unknown: {years_data[:1]}")
                raise
            logger.info(f"PHASE 1: Found {len(years)} years.")
            self.db.years.update_one({"type": "all"}, {"$set": {"values": years}}, upsert=True)

            # Filter years if target_year is specified
            if self.target_year:
                if self.target_year in years:
                    years = [self.target_year]
                    logger.info(f"Targeting specific year: {self.target_year}")
                else:
                    logger.error(f"Target year {self.target_year} not found in available years.")
                    return
            
            # Sequential Loops (Phase 2-6)
            for year in years:
                logger.info(f"\n--- Year: {year} ---")
                if not self.select_qualifier("Year", year, next_pattern="type=make"): continue
                
                makes_data = self.buffer.get(year=year, type="make")
                if not makes_data: continue
                makes = [m.get('value', m.get('Value')) for m in makes_data]
                
                for make in makes:
                    logger.info(f"  Make: {make}")
                    if not self.select_qualifier("Make", make, next_pattern="type=model"): continue
                    
                    models_data = self.buffer.get(year=year, make=make, type="model")
                    if not models_data: continue
                    models = [mo.get('value', mo.get('Value')) for mo in models_data]
                    
                    for model in models:
                        logger.info(f"    Model: {model}")
                        if not self.select_qualifier("Model", model, next_pattern="type=engine"): continue
                        
                        engines_data = self.buffer.get(year=year, make=make, model=model, type="engine")
                        if not engines_data: continue
                        engines = [e.get('value', e.get('Value')) for e in engines_data]
                        
                        for engine in engines:
                            logger.info(f"      Engine: {engine}")
                            if not self.select_qualifier("Engine", engine, next_pattern="type=submodel"): continue
                            
                            submodels_data = self.buffer.get(year=year, make=make, model=model, engine=engine, type="submodel")
                            if not submodels_data: continue
                            submodels = [s.get('value', s.get('Value')) for s in submodels_data]
                            
                            for submodel in submodels:
                                logger.info(f"        Submodel: {submodel}")
                                
                                # Check if already exists to skip heavy extraction
                                existing = self.db.vehicles.find_one({
                                    "year": year,
                                    "make": make,
                                    "model": model,
                                    "engine": engine,
                                    "submodel": submodel
                                })
                                # Only skip if it exists AND has options captured (and force is False)
                                if not self.force and existing and existing.get("options"):
                                    logger.info("          Already scraped with options. Skipping.")
                                    continue

                                if not self.select_qualifier("Submodel", submodel): continue
                                
                                # Phase 6: Options
                                # Mandatory fields as requested by user
                                options = {
                                    "FUEL TYPE": "Not Available",
                                    "ENGINE CODE": "Not Available",
                                    "BODY STYLE": "Not Available",
                                    "DRIVE TYPE": "Not Available",
                                    "TRANSFER CASE TYPE": "Not Available",
                                    "TRANSMISSION CONTROL TYPE": "Not Available",
                                    "TRANSMISSION CODE": "Not Available"
                                }
                                
                                # Only proceed to click 'Options' if we successfully selected a submodel
                                # or if 'Options' is already visible.
                                logger.info("          Extracting Options...")
                                
                                # Ensure we are on the Options tab
                                try:
                                    # Click "Options" in sidebar
                                    opt_tab = self.page.locator("#qualifierTypeSelector li:has-text('Options')")
                                    if opt_tab.is_visible():
                                        opt_tab.click(force=True)
                                        self.random_delay(2.0, 3.0)
                                    else:
                                        logger.warning("          'Options' tab not visible in sidebar.")
                                except Exception as e_opt:
                                    logger.warning(f"          Error switching to Options tab: {e_opt}")
                                
                                # Capture all fields from the right pane
                                # Use a broader selector for items
                                items = self.page.locator("#qualifierValueSelector li, #qualifierValueSelector .qualifier-container, #qualifierValueSelector div[class*='qualifier']").all()
                                logger.info(f"          Found {len(items)} items in right pane.")
                                
                                for item in items:
                                    try:
                                        # Use text_content() for a cleaner read of all text nodes
                                        full_text = item.inner_text().strip()
                                        if not full_text: continue
                                        
                                        key, val = None, None
                                        
                                        # 1. Try h1/h2 (specific to ProDemand's modern UI)
                                        label_el = item.locator("h1").first
                                        value_el = item.locator("h2").first
                                        
                                        if label_el.count() > 0 and label_el.is_visible() and value_el.count() > 0:
                                            key = label_el.inner_text().strip().upper()
                                            val = value_el.inner_text().strip()
                                        
                                        # 2. Try colon split if h1/h2 failed
                                        if not key and ":" in full_text:
                                            parts = full_text.split(":", 1)
                                            key = parts[0].strip().upper()
                                            val = parts[1].strip()
                                        
                                        # 3. Try line split
                                        if not key:
                                            lines = [l.strip() for l in full_text.split("\n") if l.strip()]
                                            if len(lines) >= 2:
                                                key = lines[0].upper()
                                                val = lines[1]
                                        
                                        if key:
                                            # Clean key
                                            if key.endswith(":"): key = key[:-1].strip()
                                            
                                            # If we have a key but empty val, check children
                                            if not val:
                                                child = item.locator("li, span, div").first
                                                if child.count() > 0:
                                                    val = child.inner_text().strip()
                                            
                                            if key and val:
                                                options[key] = val
                                                logger.info(f"            CAPTURED: {key} -> {val}")
                                                
                                    except Exception as e:
                                        logger.warning(f"            Failed item: {e}")
                                
                                # Fallback: check sidebar for other non-standard categories if they exist
                                # (e.g. if a specific platform version puts things back in the sidebar)
                                other_q = self.page.locator("#qualifierTypeSelector li").all()
                                for oq in other_q:
                                    oq_text = oq.inner_text().strip()
                                    if oq_text in ["Year", "Make", "Model", "Engine", "Submodel", "Options", "Odometer"] or oq_text in options:
                                        continue
                                    
                                    try:
                                        oq.click(force=True)
                                        self.random_delay(1.0, 2.0)
                                        val = self.page.locator("#qualifierValueSelector li.active").first.inner_text().strip()
                                        if not val: val = self.page.locator("#qualifierValueSelector li").first.inner_text().strip()
                                        options[oq_text] = val or "Not Available"
                                        logger.info(f"            {oq_text} -> {options[oq_text]}")
                                    except: pass
                                # Final vehicle record

                                vehicle = {
                                    "year": year,
                                    "make": make,
                                    "model": model,
                                    "engine": engine,
                                    "submodel": submodel,
                                    "options": options,
                                    "timestamp": time.time()
                                }
                                
                                try:
                                    self.db.vehicles.update_one(
                                        {"year": year, "make": make, "model": model, "engine": engine, "submodel": submodel},
                                        {"$set": vehicle},
                                        upsert=True
                                    )
                                    logger.info(f"          [SAVED] {year} {make} {model} {submodel}")
                                except Exception as e_db:
                                    logger.error(f"          [DB ERROR] {e_db}")

        except Exception as e:
            logger.error(f"CRITICAL ERROR: {e}")
            self.page.screenshot(path="debug_error.png")
        finally:
            self.browser.close()
            self.pw.stop()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="ProDemand Hierarchical Vehicle Selector")
    parser.add_argument("--year", help="Target year to scrape")
    parser.add_argument("--force", action="store_true", help="Force re-extraction even if data exists")
    args = parser.parse_args()
    
    scraper = VehicleScraper(headless=False, target_year=args.year, force=args.force)
    scraper.run()
