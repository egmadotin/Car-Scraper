import os
import json
import time
import random
import logging
from urllib.parse import urlparse, parse_qs
from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth
from pymongo import MongoClient

import cloudinary
import cloudinary.uploader
from bson import ObjectId

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

    def get(self, max_retries=3, **kwargs):
        # Normalize search params
        search_params = {k.lower(): str(v) for k, v in kwargs.items()}
        key_parts = []
        for k in sorted(search_params.keys()):
            key_parts.append(f"{k}={search_params[k]}")
        key = "&".join(key_parts)
        
        data = self.responses.get(key)
        # logger.info(f"      [Buffer LookUp] {key} -> {'Found' if data else 'Not Found'}")
        return data

    def clear(self):
        self.responses = {}

class VehicleScraper:
    def __init__(self, headless=False, target_year=None, force=False):
        self.pw = sync_playwright().start()
        
        # Use system Chrome and disable problematic features to help VPN/Connection
        self.browser = self.pw.chromium.launch(
            headless=headless,
            channel="chrome",
            args=[
                '--disable-features=UseDNSHttpsSvcb',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-infobars',
                '--window-position=0,0',
                '--ignore-certificate-errors',
                '--ignore-certificate-errors-spki-list'
            ]
        )
        self.session_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session.json")
        
        context_args = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "viewport": {"width": 1920, "height": 1080}
        }
        if os.path.exists(self.session_path):
            try:
                context_args["storage_state"] = self.session_path
                logger.info(f"Loading session from {self.session_path}")
            except Exception as e_session:
                logger.warning(f"Failed to load session: {e_session}")

        self.context = self.browser.new_context(**context_args)
        self.page = self.context.new_page()
        Stealth().apply_stealth_sync(self.page)
        
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
        
        # Cloudinary Setup
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET")
        )


    def random_delay(self, min_s=1.5, max_s=3.0):
        time.sleep(random.uniform(min_s, max_s))

    def login(self):
        # Check if already logged in
        logger.info("Checking session status...")
        
        # Helper to navigate with retries for connection reset
        def navigate_with_retry(url, retries=5):
            for i in range(retries):
                try:
                    logger.info(f"  Attempting to reach {url} (Attempt {i+1})...")
                    self.page.goto(url, wait_until="load", timeout=60000)
                    return True
                except Exception as e:
                    logger.warning(f"  Navigation attempt {i+1} failed: {e}")
                    # Handle common VPN/Network reset errors
                    if any(err in str(e) for err in ["ERR_CONNECTION_RESET", "ERR_CONNECTION_CLOSED", "ERR_NETWORK_CHANGED", "ERR_TIMED_OUT"]):
                        time.sleep(5 * (i + 1)) # Backoff
                        continue
                    if i == retries - 1: raise e
            return False

        try:
            # Try accessing index first (session check)
            self.page.goto("https://www.prodemand.com/Main/Index", wait_until="load", timeout=30000)
            if self.page.is_visible("#vehicleSelectionButton") or self.page.is_visible("#qualifierValueSelector") or self.page.is_visible("#ContentRegion"):
                logger.info("Already logged in via session.")
                return
        except:
            pass

        logger.info("Authenticating...")
        navigate_with_retry("https://prodemand.com/")
        
        if self.page.is_visible("#onetrust-accept-btn-handler"):
            logger.info("Accepting OneTrust cookies...")
            self.page.click("#onetrust-accept-btn-handler")
            time.sleep(2)
        elif self.page.is_visible("button:has-text('Accept All Cookies')"):
            self.page.click("button:has-text('Accept All Cookies')")
            time.sleep(2)

        if not self.page.is_visible("#username"):
            # Check if we are already on a logged-in page (e.g. dashboard)
            if self.page.is_visible("#vehicleSelectionButton") or self.page.is_visible("#ContentRegion"):
                logger.info("Detected dashboard state — already authenticated.")
                return

            # Try to find the primary login button
            login_btn = self.page.locator("#btnLogin, #btnLoginHero, .button:has-text('Login'), a:has-text('Log In')").first
            if login_btn.is_visible():
                logger.info("Clicking Login on landing page...")
                login_btn.click()
                # Wait for navigation or the username field
                try:
                    self.page.wait_for_load_state("networkidle", timeout=15000)
                except: pass
                
                # Take a screenshot right after clicking to see the transition
                self.page.screenshot(path="after_login_click.png")
            
            try:
                # Wait for navigation or the username field
                # Check for dashboard indicators first in case login was automatic (remembered session)
                try:
                    self.page.wait_for_selector("#username, #vehicleSelectionButton, #ContentRegion", timeout=60000)
                except Exception as e_wait:
                    # Final check before failing
                    if self.page.is_visible("#username"): pass
                    elif self.page.is_visible("#vehicleSelectionButton") or self.page.is_visible("#ContentRegion"):
                         logger.info("Session active after login click — already authenticated.")
                         return
                    else: raise e_wait
            except Exception as e:
                if self.page.is_closed():
                    logger.error("Browser closed during login.")
                    raise
                
                # Check for specific blocking messages
                is_expired = self.page.is_visible(".expired.container") or self.page.is_visible("text='Your account cannot access the application'")
                
                if is_expired:
                    logger.error("CRITICAL ERROR: Account access denied or session expired.")
                    if os.path.exists(self.session_path):
                        try:
                            os.remove(self.session_path)
                            logger.info(f"Deleted expired session file: {self.session_path}")
                        except: pass
                    
                    # Diagnostic screenshot
                    self.page.screenshot(path="access_denied.png")
                    
                    # Try to clear cookies and redirect to a clean login
                    logger.info("Attempting to clear session and retry login...")
                    self.context.clear_cookies()
                    self.page.goto("https://prodemand.com/", wait_until="load")
                    # Recursive call or just fail and let the user restart
                    raise Exception("Account access denied or session expired. Session cleared. Please restart the scraper.")
                
                logger.error(f"Failed to find #username: {e}")
                self.page.screenshot(path="login_timeout.png")
                raise
            
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
            
            logger.info("Login successful. Saving session...")
            self.context.storage_state(path=self.session_path)
            logger.info(f"Session saved to {self.session_path}")

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

    def click_use_vehicle(self):
        logger.info("Transitioning to 1SEARCH PLUS Dashboard...")
        try:
            self.random_delay(2, 4)
            selectors = ["#useThisVehicleButton", "input[value='Use This Vehicle']", "button:has-text('Use This Vehicle')"]
            use_btn = None
            for sel in selectors:
                el = self.page.locator(sel).first
                if el.is_visible():
                    use_btn = el
                    break
            if not use_btn:
                use_btn = self.find_element_in_frames("input[value='Use This Vehicle'], #useThisVehicleButton")

            if use_btn:
                use_btn.click(force=True)
                logger.info("  'Use This Vehicle' clicked. Waiting for dashboard...")
                # Wait for the main dashboard container
                self.page.wait_for_selector("#ContentRegion, #quickLinkRegion", timeout=45000)
                # Extra grace period for JS initialization
                self.page.wait_for_load_state("networkidle")
                self.random_delay(5, 8)
                return True
            return False
        except Exception as e:
            logger.error(f"  Failed to transition to dashboard: {e}")
            return False

    def process_quick_links(self, vehicle_id):
        logger.info("Processing Dashboard Quick Links...")
        
        # Mandatory wait for dashboard initialization
        self.random_delay(8, 12)
        
        # Wait for the quick link container specifically
        ql_container = self.find_element_in_frames("#quickLinkRegion, .quickAccess")
        if not ql_container:
            logger.warning("  [WARNING] Quick Link Region NOT found. Taking diagnostic screenshot.")
            self.page.screenshot(path="dashboard_missing_links.png")
            # Final attempt to find it after another delay
            self.random_delay(5, 8)
            ql_container = self.find_element_in_frames("#quickLinkRegion, .quickAccess")
        
        if not ql_container:
            logger.error("  [ERROR] Could not locate dashboard navigation. Skipping this vehicle.")
            return

        quick_links = [
            ("Technical Bulletins", "technicalBulletinAccess", self.handle_tsbs),
            # ("Common Specs", "commonSpecsAccess", self.handle_specs),
            # ("Driver Assist ADAS", "adasAccess", self.handle_adas),
            # ("Fluid Capacities", "fluidsQuickAccess", self.handle_fluids),
            # ("Tire Information & Lifting Points", "tpmsTireFitmentQuickAccess", self.handle_tires_lifting),
            # ("Reset Procedures", "resetProceduresAccess", self.handle_resets),
            # ("DTC Index", "dtcIndexAccess", self.handle_dtcs),
            # ("Wiring Diagrams", "wiringDiagramsAccess", self.handle_wiring),
            # ("Component Locations", "electricalComponentLocationAccess", self.handle_locations),
            # ("Component Tests", "ctmQuickAccess", self.handle_tests),
            # ("Service Manual", "serviceManualQuickAccess", self.handle_service_manual)
        ]
        
        for name, element_id, handler in quick_links:
            selector = f"#{element_id}"
            try:
                el = self.find_element_in_frames(selector)
                if el and el.is_visible():
                    is_disabled = el.get_attribute("disabled") == "disabled" or "disabled" in (el.get_attribute("class") or "").lower()
                    if is_disabled:
                        logger.info(f"    [Skip] {name} is disabled.")
                        continue
                    
                    logger.info(f"    [Click] {name}...")
                    el.click(force=True)
                    self.random_delay(4, 7)
                    
                    # Call the specific handler for this tab
                    handler(vehicle_id)
                    
                    # Close the modal after extraction
                    self.close_content_modal()
                else:
                    logger.info(f"    [Not Found] {name}")
            except Exception as e:
                logger.warning(f"    Error clicking {name}: {e}")
                self.close_content_modal()

    def handle_tsbs(self, vehicle_id):
        logger.info("    Extracting Technical Bulletins...")
        try:
            # Wait for list to appear
            self.page.wait_for_selector(".articleListContent, #categorySelectorDiv", timeout=15000)
            
            # Find all article links
            # Using a more robust selector that includes text
            links = self.page.locator(".articleListContent a, #categorySelectorDiv a").all()
            logger.info(f"      Found {len(links)} TSB items.")
            
            for index, link in enumerate(links):
                try:
                    title = link.inner_text().strip()
                    if not title:
                        # Try to get from data-name or title attribute
                        title = link.get_attribute("title") or link.get_attribute("data-name") or f"Article_{index}"
                    
                    logger.info(f"      [{index+1}/{len(links)}] Extracting: {title}")
                    
                    # Ensure no blocking modal from previous attempt
                    self.close_content_modal()
                    
                    link.scroll_into_view_if_needed()
                    link.click(force=True)
                    self.random_delay(3, 6)
                    
                    viewer = self.find_element_in_frames("#contentViewerDiv, .article-container")
                    if viewer:
                        # 1. Capture HTML and Process/Upload Images to Cloudinary
                        updated_html, images = self.process_images_in_content(viewer, vehicle_id, title)
                        
                        # 2. Store in DB
                        self.db.tsbs.update_one(
                            {"vehicle_id": vehicle_id, "title": title},
                            {
                                "$set": {
                                    "title": title,
                                    "content_html": updated_html,
                                    "images": images,
                                    "timestamp": time.time()
                                }
                            },
                            upsert=True
                        )
                    
                    self.close_content_modal()
                    self.random_delay(1, 2)
                    
                except Exception as e_inner:
                    logger.warning(f"        Failed to extract TSB '{title}': {e_inner}")
                    self.close_content_modal()

        except Exception as e:
            logger.error(f"    TSB extraction error: {e}")

    def process_images_in_content(self, container_loc, vehicle_id, title_context):
        processed_images = []
        try:
            # We'll use the HTML content and replace URLs
            content_html = container_loc.inner_html()
            
            img_els = container_loc.locator("img").all()
            for i, img in enumerate(img_els):
                src = img.get_attribute("src")
                if src and not src.startswith("data:"):
                    # Upload to Cloudinary
                    logger.info(f"        Uploading image {i+1} to Cloudinary...")
                    c_res = self.upload_image(src, folder=f"tsbs/{vehicle_id}")
                    if c_res:
                        cloud_url = c_res.get("secure_url")
                        processed_images.append({
                            "original_src": src,
                            "cloudinary_url": cloud_url,
                            "alt": img.get_attribute("alt") or ""
                        })
                        # Replace in HTML
                        content_html = content_html.replace(src, cloud_url)
            
            return content_html, processed_images
        except Exception as e:
            logger.warning(f"      Image processing error: {e}")
            return container_loc.inner_html(), []

    def upload_image(self, url, folder="general"):
        try:
            # If URL is relative, try to make it absolute or just skip if we can't
            if not url.startswith("http"):
                # Handle relative URLs if needed, but often they are full URLs from the API
                return None
            
            res = cloudinary.uploader.upload(url, folder=folder)
            return res
        except Exception as e:
            logger.warning(f"    Cloudinary upload failed: {e}")
            return None

    def handle_specs(self, vehicle_id):
        logger.info("    Extracting Specs...")
        data = self.extract_table_data("table")
        if data:
            self.db.specs.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_adas(self, vehicle_id):
        logger.info("    Extracting ADAS...")
        data = self.extract_table_data("table")
        if data:
            self.db.adas.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_fluids(self, vehicle_id):
        logger.info("    Extracting Fluids...")
        data = self.extract_table_data("table")
        if data:
            self.db.fluids.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_tires_lifting(self, vehicle_id):
        logger.info("    Extracting Tires...")
        img_el = self.find_element_in_frames("img[src*='lifting'], img[src*='tire']")
        img_url = img_el.get_attribute("src") if img_el else None
        data = self.extract_table_data("table")
        self.db.tires_lifting.update_one(
            {"vehicle_id": vehicle_id},
            {"$set": {"image_url": img_url, "specs": data}},
            upsert=True
        )

    def handle_resets(self, vehicle_id):
        logger.info("    Extracting Resets...")
        links = self.page.locator(".articleListContent a, .reset-link").all()
        for link in links:
            title = link.inner_text().strip()
            link.click()
            self.random_delay(2, 4)
            viewer = self.find_element_in_frames("#contentViewerDiv, .reset-content")
            if viewer:
                self.db.resets.update_one(
                    {"vehicle_id": vehicle_id, "procedure": title},
                    {"$set": {"content_html": viewer.inner_html()}},
                    upsert=True
                )
            self.close_content_modal()

    def handle_dtcs(self, vehicle_id):
        logger.info("    Extracting DTCs...")
        data = self.extract_table_data("table")
        if data:
            self.db.dtcs.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_wiring(self, vehicle_id):
        logger.info("    Extracting Wiring...")
        links = self.page.locator(".articleListContent a, .wiring-link").all()
        for link in links:
            name = link.inner_text().strip()
            link.click()
            self.random_delay(3, 5)
            img = self.find_element_in_frames("img[src*='diagram'], .wiring-diagram img")
            if img:
                self.db.wiring.update_one(
                    {"vehicle_id": vehicle_id, "system": name},
                    {"$set": {"diagram_url": img.get_attribute("src")}},
                    upsert=True
                )
            self.close_content_modal()

    def handle_locations(self, vehicle_id):
        logger.info("    Extracting Locations...")
        links = self.page.locator(".articleListContent a, .location-link").all()
        for link in links:
            name = link.inner_text().strip()
            link.click()
            self.random_delay(2, 4)
            img = self.find_element_in_frames("img, .location-image")
            text = self.find_element_in_frames(".location-text, .description")
            self.db.locations.update_one(
                {"vehicle_id": vehicle_id, "component": name},
                {"$set": {
                    "image_url": img.get_attribute("src") if img else None,
                    "location_text": text.inner_text().strip() if text else ""
                }},
                upsert=True
            )
            self.close_content_modal()

    def handle_tests(self, vehicle_id):
        logger.info("    Extracting Tests...")
        data = self.extract_table_data("table")
        if data:
            self.db.tests.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_service_manual(self, vehicle_id):
        logger.info("    Extracting Service Manual Tree...")
        try:
            # 1. Wait for the Service Manual panel/drawer to appear
            self.page.wait_for_selector("#slidePanelContent, #categorySelectorDiv", timeout=20000)
            
            # 2. Ensure "Table of Contents" tab is selected
            toc_tab = self.page.locator("#serviceManualDrawerTabs li:has-text('Table of Contents')").first
            if toc_tab.is_visible() and "selected" not in (toc_tab.get_attribute("class") or ""):
                toc_tab.click()
                self.random_delay(1.0, 2.0)

            # 3. Recursive extraction
            self._scrape_manual_tree(vehicle_id, path=[])
            
        except Exception as e:
            logger.error(f"      Service Manual extraction failed: {e}")

    def _scrape_manual_tree(self, vehicle_id, path):
        # Find all nodes at the current level that are children of the last path element
        # In this UI, nodes are usually within the visible tree
        try:
            # Get top-level nodes if path is empty, otherwise find sub-nodes
            if not path:
                nodes = self.page.locator("#categorySelectorDiv > ul > li").all()
            else:
                # Find the parent LI based on the last path name
                parent_li = self.page.locator(f"li:has(> a:text-is('{path[-1]}'))").first
                nodes = parent_li.locator("> ul > li").all()

            for node in nodes:
                node_text = node.locator("> a").inner_text().strip()
                is_branch = "branch" in (node.get_attribute("class") or "")
                is_leaf = "leaf" in (node.get_attribute("class") or "")
                
                new_path = path + [node_text]
                
                if is_branch:
                    # Expand if closed
                    if "closed" in (node.get_attribute("class") or ""):
                        logger.info(f"      [Expand] {' > '.join(new_path)}")
                        node.locator("> .treeExpandCollapseIcon").click()
                        self.random_delay(0.5, 1.5)
                    
                    # Recurse
                    self._scrape_manual_tree(vehicle_id, new_path)
                
                elif is_leaf:
                    logger.info(f"      [Article] {' > '.join(new_path)}")
                    
                    # Check if already scraped (optional optimization)
                    existing = self.db.manuals.find_one({"vehicle_id": vehicle_id, "path": new_path})
                    if existing and not self.force:
                        continue

                    # Click and Extract
                    node.locator("> a").click()
                    self.random_delay(3, 6)
                    
                    viewer = self.find_element_in_frames("#contentViewerDiv, #ContentRegion .article-container")
                    if viewer:
                        content_html = viewer.inner_html()
                        images = self.extract_images_from_content(viewer)
                        tables = self.extract_tables_from_content(viewer)
                        
                        self.db.manuals.update_one(
                            {"vehicle_id": vehicle_id, "path": new_path},
                            {
                                "$set": {
                                    "title": node_text,
                                    "content_html": content_html,
                                    "images": images,
                                    "tables": tables,
                                    "timestamp": time.time()
                                }
                            },
                            upsert=True
                        )
                    
                    # Return to tree focus if needed (drawer might close or overlay)
                    if not self.page.is_visible("#categorySelectorDiv"):
                        self.page.click("#slidePanelHandle", force=True) # Re-open drawer
                        self.random_delay(1, 2)

        except Exception as e:
            logger.warning(f"        Error in tree traversal: {e}")

    def extract_images_from_content(self, viewer_loc):
        images = []
        try:
            img_elements = viewer_loc.locator("img").all()
            for img in img_elements:
                src = img.get_attribute("src")
                alt = img.get_attribute("alt") or ""
                if src and not src.startswith("data:"):
                    # We could upload to cloudinary here if needed, but for now just record original
                    images.append({
                        "original_src": src,
                        "alt": alt
                    })
        except: pass
        return images

    def extract_tables_from_content(self, viewer_loc):
        tables = []
        try:
            table_elements = viewer_loc.locator("table").all()
            for table in table_elements:
                rows = []
                tr_elements = table.locator("tr").all()
                for tr in tr_elements:
                    cells = [c.inner_text().strip() for c in tr.locator("td, th").all()]
                    if cells: rows.append(cells)
                if rows: tables.append(rows)
        except: pass
        return tables

    def extract_table_data(self, selector):
        data = []
        try:
            table = self.find_element_in_frames(selector)
            if not table: return []
            rows = table.locator("tr").all()
            if not rows: return []
            headers = [h.inner_text().strip().upper() for h in rows[0].locator("th, td").all()]
            for row in rows[1:]:
                cells = [c.inner_text().strip() for c in row.locator("td").all()]
                if len(cells) == len(headers):
                    data.append({headers[i]: cells[i] for i in range(len(headers))})
        except: pass
        return data

    def close_content_modal(self):
        try:
            # Common close buttons for Mitchell1/ProDemand modals
            close_selectors = ["#contentViewerDiv .close-button", ".x-btn", ".modal-close", ".close-icon", "button:has-text('Close')"]
            for sel in close_selectors:
                btn = self.find_element_in_frames(sel)
                if btn and btn.is_visible():
                    btn.click()
                    time.sleep(1)
                    break
        except: pass

    def find_element_in_frames(self, selector):
        # 1. Check main page
        try:
            el = self.page.locator(selector).first
            if el.is_visible():
                return el
        except: pass
        
        # 2. Check all frames
        for frame in self.page.frames:
            try:
                # Basic check for accessibility
                if frame.is_detached(): continue
                el = frame.locator(selector).first
                if el.is_visible():
                    return el
            except: continue
        return None

    def wait_for_ready(self):
        logger.info("Waiting for application to be ready...")
        try:
            # Wait for body to at least exist, then give it a grace period
            self.page.wait_for_selector("body", timeout=30000)
            self.random_delay(5, 8)
            return True
        except Exception as e:
            logger.error(f"Application ready check timed out: {e}")
            return False

    def return_to_selector(self):
        logger.info("Returning to vehicle selector...")
        try:
            # 1. Click 'Change Vehicle' in header to open dropdown
            selectors_change = [".change-vehicle", "div:has-text('Change Vehicle')", "button:has-text('Change Vehicle')", "#vehicleSelectionButton"]
            for sel in selectors_change:
                btn = self.page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    self.random_delay(1.5, 2.5)
                    break
            
            # 2. Click 'Vehicle Selection' item in dropdown
            selectors_select = ["h1:has-text('Vehicle Selection')", "div:has-text('Vehicle Selection')", ".vehicleSelectionButton", "text='Select Vehicle'"]
            for sel in selectors_select:
                btn = self.page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    break
            
            # 3. Wait for selector to appear
            try:
                self.page.wait_for_selector("#qualifierValueSelector", timeout=15000)
                return True
            except:
                # Fallback: navigate directly to selector URL
                logger.info("  Selector modal didn't appear. Navigating to Index...")
                self.page.goto("https://www1.prodemand.com/Main/Index", wait_until="load")
                self.page.wait_for_selector("#qualifierValueSelector", timeout=30000)
                return True
        except Exception as e:
            logger.error(f"Failed to return to selector: {e}")
            return False


    def run(self):
        try:
            self.login()
            
            if not self.wait_for_ready():
                logger.warning("Application ready check timed out. Proceeding with caution.")
            
            # 1. Ensure we are on the vehicle selector
            # If ContentRegion is visible, we are likely on the dashboard
            if self.page.is_visible("#ContentRegion") or not self.page.is_visible("#qualifierValueSelector"):
                logger.info("Ensuring we are on the vehicle selector...")
                if not self.page.is_visible("#qualifierValueSelector"):
                    self.return_to_selector()

            # 2. Final wait for the selector to be ready
            try:
                self.page.wait_for_selector("#qualifierValueSelector", timeout=30000)
            except Exception as e_sel:
                logger.error(f"Vehicle selector (#qualifierValueSelector) not found: {e_sel}")
                # Take diagnostic screenshot
                self.page.screenshot(path="selector_not_found.png")
                raise
            
            # Phase 1: Years
            logger.info("PHASE 1: Fetching available years...")
            self.page.click("#qualifierTypeSelector li:has-text('Year')", force=True)
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
                if not makes_data:
                    # Retry selection once
                    logger.warning(f"  [Retry] Buffer empty for Year {year} Makes. Re-selecting...")
                    self.select_qualifier("Year", year, next_pattern="type=make")
                    self.random_delay(3, 5)
                    makes_data = self.buffer.get(year=year, type="make")
                
                if not makes_data:
                    logger.error(f"  [Skip] No makes found for Year {year}")
                    continue
                    
                makes = [m.get('value', m.get('Value')) for m in makes_data]
                
                for make in makes:
                    logger.info(f"  Make: {make}")
                    if not self.select_qualifier("Make", make, next_pattern="type=model"): continue
                    
                    models_data = self.buffer.get(year=year, make=make, type="model")
                    if not models_data:
                        logger.warning(f"    [Retry] Buffer empty for {make} Models. Re-selecting...")
                        self.select_qualifier("Make", make, next_pattern="type=model")
                        self.random_delay(3, 5)
                        models_data = self.buffer.get(year=year, make=make, type="model")
                    
                    if not models_data: continue
                    models = [mo.get('value', mo.get('Value')) for mo in models_data]
                    
                    for model in models:
                        logger.info(f"    Model: {model}")
                        if not self.select_qualifier("Model", model, next_pattern="type=engine"): continue
                        
                        engines_data = self.buffer.get(year=year, make=make, model=model, type="engine")
                        if not engines_data:
                            logger.warning(f"      [Retry] Buffer empty for {model} Engines. Re-selecting...")
                            self.select_qualifier("Model", model, next_pattern="type=engine")
                            self.random_delay(3, 5)
                            engines_data = self.buffer.get(year=year, make=make, model=model, type="engine")
                        
                        if not engines_data: continue
                        engines = [e.get('value', e.get('Value')) for e in engines_data]
                        
                        for engine in engines:
                            logger.info(f"      Engine: {engine}")
                            if not self.select_qualifier("Engine", engine, next_pattern="type=submodel"): continue
                            
                            submodels_data = self.buffer.get(year=year, make=make, model=model, engine=engine, type="submodel")
                            if not submodels_data:
                                logger.warning(f"        [Retry] Buffer empty for {engine} Submodels. Re-selecting...")
                                self.select_qualifier("Engine", engine, next_pattern="type=submodel")
                                self.random_delay(3, 5)
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
                                    res = self.db.vehicles.update_one(
                                        {"year": year, "make": make, "model": model, "engine": engine, "submodel": submodel},
                                        {"$set": vehicle},
                                        upsert=True
                                    )
                                    logger.info(f"          [SAVED] {year} {make} {model} {submodel}")
                                    
                                    if self.click_use_vehicle():
                                        v_rec = self.db.vehicles.find_one({"year": year, "make": make, "model": model, "engine": engine, "submodel": submodel})
                                        if v_rec:
                                            self.process_quick_links(v_rec["_id"])
                                        self.return_to_selector()
                                        
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
