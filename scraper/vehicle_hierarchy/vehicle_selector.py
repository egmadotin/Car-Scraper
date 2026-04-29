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
        self.session_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "session.json")
        
        context_args = {
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
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
        try:
            self.page.goto("https://www.prodemand.com/Main/Index", wait_until="load", timeout=30000)
            if self.page.is_visible("#vehicleSelectionButton") or self.page.is_visible("#qualifierValueSelector") or self.page.is_visible("#ContentRegion"):
                logger.info("Already logged in via session.")
                return
        except:
            pass

        logger.info("Authenticating...")
        for attempt in range(3):
            try:
                # Use the user-provided landing page
                self.page.goto("https://prodemand.com/", wait_until="load", timeout=60000)
                break
            except Exception as e:
                logger.warning(f"  Login goto attempt {attempt+1} failed: {e}")
                if attempt == 2: raise
                time.sleep(5)
        
        if self.page.is_visible("#onetrust-accept-btn-handler"):
            logger.info("Accepting OneTrust cookies...")
            self.page.click("#onetrust-accept-btn-handler")
            time.sleep(2)
        elif self.page.is_visible("button:has-text('Accept All Cookies')"):
            self.page.click("button:has-text('Accept All Cookies')")
            time.sleep(2)

        if not self.page.is_visible("#username"):
            # Try to find the primary login button
            login_btn = self.page.locator("#btnLogin, #btnLoginHero, .button:has-text('Login')").first
            if login_btn.is_visible():
                logger.info("Clicking Login on landing page...")
                login_btn.click()
                # Take a screenshot right after clicking to see the transition
                time.sleep(5)
                self.page.screenshot(path="after_login_click.png")
            
            try:
                # Wait for navigation or the username field
                # Mitchell1 login is usually on a different domain, so wait_for_url might be useful
                self.page.wait_for_selector("#username", timeout=60000)
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
            # Click "Use This Vehicle"
            use_btn = self.page.locator("input[value='Use This Vehicle'], .button.blue").first
            if use_btn.is_visible():
                use_btn.click()
                # Wait for dashboard to load (1SEARCH PLUS is usually the first indicator)
                self.page.wait_for_selector("#ContentRegion, .dashboard-tabs", timeout=60000)
                logger.info("  Dashboard loaded.")
                return True
            else:
                logger.warning("  'Use This Vehicle' button not found.")
                return False
        except Exception as e:
            logger.error(f"  Failed to transition to dashboard: {e}")
            return False

    def find_element_in_frames(self, selector):
        try:
            el = self.page.locator(selector).first
            if el.is_visible(): return el
        except: pass
        
        for frame in self.page.frames:
            try:
                el = frame.locator(selector).first
                if el.is_visible(): return el
            except: continue
        return None

    def extract_all_tabs(self, vehicle_id):
        logger.info(f"Extracting technical data for vehicle {vehicle_id}...")
        
        # Wait for dashboard content to settle
        self.random_delay(3, 5)
        
        # 1. Capture Analytics from Landing Page
        self.extract_dashboard_analytics(vehicle_id)
        
        # 2. Tab Routing
        tabs = [
            ("Technical Bulletins", self.handle_tsbs),
            ("Common Specs", self.handle_specs),
            ("Driver Assist ADAS", self.handle_adas),
            ("Fluid Capacities", self.handle_fluids),
            ("Tire Information & Lifting Points", self.handle_tires_lifting),
            ("Reset Procedures", self.handle_resets),
            ("DTC Index", self.handle_dtcs),
            ("Wiring Diagrams", self.handle_wiring),
            ("Component Locations", self.handle_locations),
            ("Component Tests", self.handle_tests),
            ("Service Manual", self.handle_service_manual)
        ]
        
        for tab_name, handler in tabs:
            try:
                logger.info(f"  [Tab] Processing {tab_name}...")
                
                # Search across all frames for the tab
                target = self.find_element_in_frames(f"xpath=//div[contains(., '{tab_name}') and contains(@class, 'tab')]")
                if not target:
                    target = self.find_element_in_frames(f"xpath=//span[contains(text(), '{tab_name}')]/parent::div")
                if not target:
                    target = self.find_element_in_frames(f"text='{tab_name}'")

                if target and target.is_visible():
                    # Check if it's disabled
                    is_disabled = "disabled" in (target.get_attribute("class") or "").lower() or "gray" in (target.get_attribute("style") or "").lower()
                    if is_disabled:
                        logger.info(f"    Tab {tab_name} is disabled (grayed out).")
                        continue

                    logger.info(f"    Clicking tab: {tab_name}")
                    target.click(force=True)
                    self.page.wait_for_load_state("networkidle")
                    self.random_delay(2, 4)
                    handler(vehicle_id)
                    self.close_content_modal()
                else:
                    logger.info(f"    Tab {tab_name} not found.")
            except Exception as e:
                logger.error(f"    Error processing tab {tab_name}: {e}")
                self.close_content_modal()

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
            for sel in [".change-vehicle", "div:has-text('Change Vehicle')", "button:has-text('Change Vehicle')"]:
                btn = self.page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    self.random_delay(1.0, 2.0)
                    break
            
            # 2. Click 'Vehicle Selection' item in dropdown
            for sel in ["h1:has-text('Vehicle Selection')", "div:has-text('Vehicle Selection')", ".vehicleSelectionButton", "text='Select Vehicle'"]:
                btn = self.page.locator(sel).first
                if btn.is_visible():
                    btn.click()
                    self.page.wait_for_selector("#qualifierValueSelector", timeout=30000)
                    return True
            
            # Fallback: navigate directly to selector URL
            self.page.goto("https://www1.prodemand.com/Main/Index", wait_until="networkidle")
            self.page.wait_for_selector("#qualifierValueSelector", timeout=30000)
            return True
        except Exception as e:
            logger.error(f"Failed to return to selector: {e}")
            return False

    def close_content_modal(self):
        try:
            # Close button in the #contentViewerDiv
            close_btn = self.page.locator("#contentViewerDiv .close-button, #contentViewerDiv .x-btn").first
            if close_btn.is_visible():
                close_btn.click()
                time.sleep(1)
        except: pass

    def extract_dashboard_analytics(self, vehicle_id):
        logger.info("  Extracting landing page analytics...")
        summary = {}
        
        sections = {
            "Commonly Replaced COMPONENTS": "components",
            "Common DTCs": "dtcs",
            "Common SYMPTOMS": "symptoms",
            "Top Search LOOKUPS": "lookups"
        }
        
        for section_title, db_key in sections.items():
            try:
                # Find the section by text (usually in an h3 or bold div)
                section_header = self.find_element_in_frames(f"xpath=//h3[contains(., '{section_title}')] or //div[contains(., '{section_title}')]")
                if not section_header:
                    section_header = self.find_element_in_frames(f"text='{section_title}'")
                
                if section_header and section_header.is_visible():
                    # The parent or next sibling container usually has the list
                    container = section_header.locator("xpath=..")
                    content = container.inner_text().strip()
                    
                    # Remove the title from the content to get just the list
                    content = content.replace(section_title, "").strip()
                    
                    if "We are busy collecting" in content or not content:
                        summary[db_key] = []
                    else:
                        # Split by newlines and clean up
                        items = [i.strip() for i in content.split("\n") if i.strip() and len(i.strip()) > 1]
                        summary[db_key] = items
                        logger.info(f"    Captured {len(items)} {db_key}")
            except Exception as e:
                logger.warning(f"    Failed to extract {section_title}: {e}")
        
        if summary:
            self.db.vehicles.update_one({"_id": vehicle_id}, {"$set": {"dashboard_summary": summary}})

    def extract_table_data(self, selector):
        """Converts an HTML table into a list of dicts."""
        data = []
        try:
            # Search in frames for the table
            table = self.find_element_in_frames(selector)
            if not table: return []
            
            rows = table.locator("tr").all()
            if not rows: return []
            
            # Identify headers
            headers = [h.inner_text().strip().upper() for h in rows[0].locator("th, td").all()]
            start_idx = 1 if any(h for h in headers) else 0
            if start_idx == 0:
                 headers = [f"COL_{i}" for i in range(len(headers))]
            
            for row in rows[start_idx:]:
                cells = [c.inner_text().strip() for c in row.locator("td").all()]
                if len(cells) == len(headers):
                    row_data = {headers[i]: cells[i] for i in range(len(headers))}
                    data.append(row_data)
        except Exception as e:
            logger.warning(f"    Table extraction error: {e}")
        return data

    def handle_fluids(self, vehicle_id):
        logger.info("    Extracting Fluids...")
        try:
            # Search across frames for table
            fluids = self.extract_table_data("table, .fluid-table")
            if fluids:
                self.db.fluids.insert_many([{"vehicle_id": vehicle_id, **f} for f in fluids])
                logger.info(f"      Saved {len(fluids)} fluid records.")
        except Exception as e:
            logger.warning(f"      Fluid extraction failed: {e}")

    def handle_tsbs(self, vehicle_id):
        logger.info("    Extracting TSBs...")
        try:
            # Look for links in any frame
            links_sel = ".articleListContent a, .tsb-category-link, a[href*='article']"
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(links_sel).all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            logger.info(f"      Found {len(links)} TSB links.")
            for link_el, title in links:
                try:
                    link_el.click()
                    self.random_delay(2, 3)
                    viewer = self.find_element_in_frames("#contentViewerDiv, .article-content, #articleBody")
                    if viewer:
                        content = viewer.inner_html()
                        self.db.tsbs.insert_one({"vehicle_id": vehicle_id, "title": title, "content_html": content})
                    self.close_content_modal()
                except: pass
        except Exception as e:
            logger.warning(f"      TSB extraction error: {e}")

    def handle_service_manual(self, vehicle_id):
        logger.info("    Extracting Service Manual (Recursive Tree)...")
        try:
            tree = self.find_element_in_frames("#categorySelectorDiv, .manual-tree-container")
            if tree:
                self._scrape_manual_tree(vehicle_id, [])
            else:
                logger.warning("      Manual tree container not found.")
        except Exception as e:
            logger.warning(f"      Manual tree error: {e}")

    def _scrape_manual_tree(self, vehicle_id, path):
        # Find tree in frames
        tree_container = self.find_element_in_frames("#categorySelectorDiv")
        if not tree_container: return

        nodes = tree_container.locator("li").all()
        for i in range(len(nodes)):
            node = tree_container.locator("li").nth(i)
            text = node.inner_text().strip().split("\n")[0]
            
            # If it's a folder
            if "tree-node-collapsed" in (node.get_attribute("class") or ""):
                node.click()
                self.random_delay(1.0, 1.5)
                self._scrape_manual_tree(vehicle_id, path + [text])
            
            # If it's an article
            elif "article" in (node.get_attribute("class") or "") or node.locator("a").count() > 0:
                article_link = node.locator("a").first
                article_title = article_link.inner_text().strip()
                article_link.click()
                self.random_delay(2, 3)
                viewer = self.find_element_in_frames("#contentViewerDiv, .manual-content")
                if viewer:
                    content = viewer.inner_html()
                    self.db.manuals.insert_one({"vehicle_id": vehicle_id, "path": path, "title": article_title, "content_html": content})
                self.close_content_modal()
                
    def handle_specs(self, vehicle_id):
        logger.info("    Extracting Specs...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator("#contentViewerDiv a, .spec-topic-link, a[href*='spec']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, topic_name in links:
                try:
                    link_el.click()
                    self.random_delay(2, 3)
                    data = self.extract_table_data("table")
                    if data:
                        self.db.specs.insert_many([{"vehicle_id": vehicle_id, "topic": topic_name, **s} for s in data])
                except: pass
        except Exception as e:
            logger.warning(f"      Specs extraction failed: {e}")

    def handle_adas(self, vehicle_id):
        logger.info("    Extracting ADAS...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(".featuresList li, .adas-feature, a[href*='adas']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, feat_name in links:
                try:
                    link_el.click()
                    self.random_delay(2, 4)
                    data = self.extract_table_data("table")
                    if data:
                        self.db.adas.insert_many([{"vehicle_id": vehicle_id, "feature": feat_name, **d} for d in data])
                except: pass
        except: pass

    def handle_tires_lifting(self, vehicle_id):
        logger.info("    Extracting Tire/Lifting Info...")
        try:
            img_el = self.find_element_in_frames("img[src*='lifting'], img[src*='tire']")
            img_url = self.upload_image_el(img_el) if img_el else None
            data = self.extract_table_data("table")
            self.db.tires_lifting.insert_one({"vehicle_id": vehicle_id, "image_url": img_url, "specs": data})
        except: pass

    def handle_resets(self, vehicle_id):
        logger.info("    Extracting Reset Procedures...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(".articleListContent a, .reset-link, a[href*='reset']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, title in links:
                try:
                    link_el.click()
                    self.random_delay(2, 3)
                    viewer = self.find_element_in_frames("#contentViewerDiv, .reset-content")
                    if viewer:
                        self.db.resets.insert_one({"vehicle_id": vehicle_id, "procedure": title, "content_html": viewer.inner_html()})
                    self.close_content_modal()
                except: pass
        except: pass

    def handle_dtcs(self, vehicle_id):
        logger.info("    Extracting DTC Index...")
        data = self.extract_table_data("table")
        if data:
            self.db.dtcs.insert_many([{"vehicle_id": vehicle_id, **d} for d in data])

    def handle_wiring(self, vehicle_id):
        logger.info("    Extracting Wiring Diagrams...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(".articleListContent a, .wiring-link, a[href*='diagram']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, sys_name in links:
                try:
                    link_el.click()
                    self.random_delay(3, 5)
                    target = self.find_element_in_frames("img[src*='diagram'], svg, .wiring-diagram")
                    if target:
                        img_url = self.upload_image_el(target)
                        self.db.wiring.insert_one({"vehicle_id": vehicle_id, "system": sys_name, "diagram_url": img_url})
                    self.close_content_modal()
                except: pass
        except: pass

    def handle_locations(self, vehicle_id):
        logger.info("    Extracting Component Locations...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(".articleListContent a, .location-link, a[href*='location']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, loc_name in links:
                try:
                    link_el.click()
                    self.random_delay(2, 4)
                    img_el = self.find_element_in_frames("img, .location-image")
                    text_el = self.find_element_in_frames(".location-text, .description")
                    img_url = self.upload_image_el(img_el) if img_el else None
                    text = text_el.inner_text().strip() if text_el else ""
                    self.db.locations.insert_one({"vehicle_id": vehicle_id, "component": loc_name, "location_text": text, "image_url": img_url})
                    self.close_content_modal()
                except: pass
        except: pass

    def handle_tests(self, vehicle_id):
        logger.info("    Extracting Component Tests...")
        try:
            links = []
            for frame in self.page.frames:
                try:
                    f_links = frame.locator(".articleListContent a, .test-link, a[href*='test']").all()
                    for fl in f_links:
                        links.append((fl, fl.inner_text().strip()))
                except: continue
                
            for link_el, test_name in links:
                try:
                    link_el.click()
                    self.random_delay(2, 4)
                    data = self.extract_table_data("table")
                    viewer = self.find_element_in_frames("#contentViewerDiv, .test-content")
                    content = viewer.inner_html() if viewer else ""
                    self.db.tests.insert_one({"vehicle_id": vehicle_id, "test": test_name, "data": data, "content_html": content})
                    self.close_content_modal()
                except: pass
        except: pass

    def upload_image(self, selector):
        try:
            img = self.page.locator(selector).first
            if img.is_visible():
                src = img.get_attribute("src")
                if src and src.startswith("http"):
                    upload_res = cloudinary.uploader.upload(src)
                    return upload_res.get("secure_url")
        except: pass
        return None

    def run(self):
        try:
            self.login()
            
            # Start Selector
            if not self.page.is_visible("#qualifierValueSelector"):
                if self.page.is_visible("#ContentRegion"):
                    logger.info("Already on dashboard. Returning to selector...")
                    self.page.click("#vehicleSelectionButton")
                else:
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
            
            if not self.wait_for_ready():
                raise Exception("Failed to reach a ready state after login.")
            
            # Check if we are on the dashboard instead of the selector
            if self.find_element_in_frames("#ContentRegion"):
                logger.info("Landed on dashboard after login. Returning to selector...")
                self.return_to_selector()

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
                                    res = self.db.vehicles.update_one(
                                        {"year": year, "make": make, "model": model, "engine": engine, "submodel": submodel},
                                        {"$set": vehicle},
                                        upsert=True
                                    )
                                    logger.info(f"          [SAVED] {year} {make} {model} {submodel}")
                                    
                                    # --- Dashboard Deep Dive ---
                                    if self.click_use_vehicle():
                                        # Get the ID for linking
                                        v_rec = self.db.vehicles.find_one({"year": year, "make": make, "model": model, "engine": engine, "submodel": submodel})
                                        if v_rec:
                                            self.extract_all_tabs(v_rec["_id"])
                                        
                                        # Return to selector for next vehicle
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
