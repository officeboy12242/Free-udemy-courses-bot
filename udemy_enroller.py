"""
Udemy Course Auto-Enroller - Scrapes coupon sites and enrolls in free/discounted courses.
Supports multiple coupon sources with expiry checking.
"""

import logging
import re
import concurrent.futures
import requests
from datetime import datetime
from urllib.parse import unquote
from html import unescape
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi_requests
    CURL_CFFI_AVAILABLE = True
except ImportError:
    CURL_CFFI_AVAILABLE = False
    cffi_requests = requests

log = logging.getLogger(__name__)


class Course:
    """Represents a Udemy course with coupon info"""
    def __init__(self, title: str, url: str, coupon_code: str = None, expires_at: str = None):
        self.title = title
        self.url = url
        self.coupon_code = coupon_code
        self.expires_at = expires_at
        self.is_expired = False
        self.course_id = None
        
        if expires_at:
            self._check_expiry()
    
    def _check_expiry(self):
        """Check if coupon has expired"""
        if not self.expires_at:
                return
        
        try:
            exp_date = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            self.is_expired = datetime.now(exp_date.tzinfo) > exp_date
        except Exception as e:
            log.warning(f"Could not parse expiry date {self.expires_at}: {e}")
    
    def is_valid(self) -> bool:
        """Check if course is valid and not expired"""
        return not self.is_expired and "udemy.com" in self.url
    
    def __repr__(self):
        return f"Course({self.title}, expires={self.expires_at}, expired={self.is_expired})"


class UdemyScraper:
    """Scrapes Udemy courses from multiple coupon sites"""
    
    def __init__(self):
        self.courses = []
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
    
    def _fetch_page(self, url: str, **kwargs):
        """Fetch page with curl_cffi for Cloudflare bypass"""
        try:
            if CURL_CFFI_AVAILABLE:
                return cffi_requests.get(
                    url, 
                    impersonate="chrome", 
                    headers=self.headers, 
                    timeout=15,
                    verify=False,
                    **kwargs
                )
            else:
                return requests.get(
                    url, 
                    headers=self.headers, 
                    timeout=15,
                    verify=False,
                    **kwargs
                )
        except Exception as e:
            log.error(f"Failed to fetch {url}: {e}")
            return None
    
    def _parse_html(self, content):
        """Parse HTML content"""
        return BeautifulSoup(content, "html.parser")
    
    def scrape_discudemy(self) -> list:
        """Scrape DiscUdemy.com (Pages 1-10)"""
        courses = []
        try:
            log.info("Scraping DiscUdemy...")
            all_items = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = [
                    executor.submit(
                        self._fetch_page,
                        f"https://www.discudemy.com/all/{page}"
                    )
                    for page in range(1, 6)  # Reduced from 11 to 6 pages for speed
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    resp = future.result()
                    if not resp or resp.status_code != 200:
                        continue
                    soup = self._parse_html(resp.content)
                    page_items = soup.find_all("a", {"class": "card-header"})
                    all_items.extend(page_items)
            
            # Fetch course details
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                futures = []
                for item in all_items:
                    title = item.string
                    url_slug = item.get("href", "").split("/")[-1]
                    futures.append((title, executor.submit(
                        self._fetch_page,
                        f"https://www.discudemy.com/go/{url_slug}"
                    )))
                
                for title, future in futures:
                    resp = future.result()
                    if not resp:
                        continue
                    try:
                        soup = self._parse_html(resp.content)
                        link = soup.find("div", {"class": "ui segment"})
                        if link and link.a:
                            udemy_link = link.a.get("href", "")
                            if "udemy.com" in udemy_link:
                                courses.append(Course(title, udemy_link))
                    except Exception as e:
                        log.debug(f"Error parsing DiscUdemy course: {e}")
            
            log.info(f"DiscUdemy: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"DiscUdemy scraping failed: {e}")
            return courses
    
    def scrape_udemyfreebies(self) -> list:
        """Scrape UdemyFreebies.com (Pages 1-3)"""
        courses = []
        try:
            log.info("Scraping UdemyFreebies...")
            all_items = []

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self._fetch_page,
                        f"https://www.udemyfreebies.com/free-udemy-courses/{page}"
                    )
                    for page in range(1, 4)  # Reduced to 3 pages
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    resp = future.result()
                    if not resp or resp.status_code != 200:
                        continue
                    soup = self._parse_html(resp.content)
                    page_items = soup.find_all("a", {"class": "theme-img"})
                    all_items.extend(page_items)
            
            # Fetch course details
            for item in all_items:
                try:
                    title = item.img.get("alt", "Unknown")
                    href_part = item.get("href", "").split("/")[4] if item.get("href") else None
                    if href_part:
                        redirect_url = f"https://www.udemyfreebies.com/out/{href_part}"
                        resp = self._fetch_page(redirect_url, allow_redirects=True)
                        if resp and "udemy.com" in resp.url:
                            courses.append(Course(title, resp.url))
                except Exception as e:
                    log.debug(f"Error parsing UdemyFreebies course: {e}")
            
            log.info(f"UdemyFreebies: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"UdemyFreebies scraping failed: {e}")
            return courses
    
    def scrape_tutorialbar(self) -> list:
        """Scrape TutorialBar.com via WP-JSON API"""
        courses = []
        try:
            log.info("Scraping TutorialBar...")
            all_items = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self._fetch_page,
                        f"https://www.tutorialbar.com/wp-json/wp/v2/posts?categories=55&per_page=100&page={page}"
                    )
                    for page in range(1, 4)
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    resp = future.result()
                    if not resp or resp.status_code != 200:
                        continue
                    data = resp.json()
                    if data:
                        all_items.extend(data)
            
            for item in all_items:
                try:
                    title = item.get("title", {}).get("rendered", "Unknown")
                    link = item.get("acf", {}).get("course_url", "")
                    if "udemy.com" in link:
                        courses.append(Course(title, link))
                except Exception as e:
                    log.debug(f"Error parsing TutorialBar course: {e}")
            
            log.info(f"TutorialBar: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"TutorialBar scraping failed: {e}")
            return courses
    
    def scrape_realdiscount(self) -> list:
        """Scrape Real.Discount API"""
        courses = []
        try:
            log.info("Scraping RealDiscount...")
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Host": "cdn.real.discount",
                "referer": "https://www.real.discount/",
            }
            
            if CURL_CFFI_AVAILABLE:
                resp = cffi_requests.get(
                    "https://cdn.real.discount/api/courses?page=1&limit=500&sortBy=sale_start&store=Udemy&freeOnly=true",
                    impersonate="chrome",
                    headers=headers,
                    timeout=15
                )
            else:
                resp = requests.get(
                    "https://cdn.real.discount/api/courses?page=1&limit=500&sortBy=sale_start&store=Udemy&freeOnly=true",
                    headers=headers,
                    timeout=15
                )
            
            if resp and resp.status_code == 200:
                data = resp.json()
                items = data.get("items", [])
                
                for item in items:
                    if item.get("store") != "Sponsored":
                        title = item.get("name", "Unknown")
                        link = item.get("url", "")
                if link:
                            courses.append(Course(title, link))
            
            log.info(f"RealDiscount: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"RealDiscount scraping failed: {e}")
            return courses
    
    def scrape_coursevania(self) -> list:
        """Scrape CourseVania.com"""
        courses = []
        try:
            log.info("Scraping CourseVania...")
            resp = self._fetch_page("https://coursevania.com/courses/")
            if not resp or resp.status_code != 200:
                return courses

            try:
                nonce = re.search(
                    r"load_content\"\:\"(.*?)\"", resp.text, re.DOTALL
                ).group(1)
            except (AttributeError, IndexError):
                log.warning("CourseVania: Nonce not found")
                return courses
            
            api_resp = self._fetch_page(
                f"https://coursevania.com/wp-admin/admin-ajax.php?template=courses/grid&args={{'posts_per_page':'500'}}&action=stm_lms_load_content&sort=date_high&nonce={nonce}"
            )
            
            if api_resp:
                data = api_resp.json()
                soup = self._parse_html(data.get("content", ""))
                page_items = soup.find_all("div", {"class": "stm_lms_courses__single--title"})
                
                for item in page_items:
                    try:
                        title = item.h5.string if item.h5 else "Unknown"
                        link_elem = item.find("a")
                        if link_elem:
                            course_url = link_elem.get("href", "")
                            course_resp = self._fetch_page(course_url)
                            if course_resp:
                                course_soup = self._parse_html(course_resp.content)
                                udemy_link = course_soup.find("a", {"class": "masterstudy-button-affiliate__link"})
                                if udemy_link and "udemy.com" in udemy_link.get("href", ""):
                                    courses.append(Course(title, udemy_link.get("href")))
                    except Exception as e:
                        log.debug(f"Error parsing CourseVania course: {e}")
            
            log.info(f"CourseVania: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"CourseVania scraping failed: {e}")
            return courses
    
    def scrape_enext(self) -> list:
        """Scrape E-Next (jobs.e-next.in)"""
        courses = []
        try:
            log.info("Scraping E-Next...")
            all_items = []
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self._fetch_page,
                        f"https://jobs.e-next.in/course/udemy/{page}"
                    )
                    for page in range(1, 4)
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    resp = future.result()
                    if not resp or resp.status_code != 200:
                        continue
                    soup = self._parse_html(resp.content)
                    page_items = soup.find_all("a", {"class": "btn btn-secondary btn-sm btn-block"})
                    all_items.extend(page_items)
            
            for item in all_items:
                try:
                    course_resp = self._fetch_page(item.get("href", ""))
                    if course_resp:
                        soup = self._parse_html(course_resp.content)
                        title_elem = soup.find("h3")
                        title = title_elem.string.strip() if title_elem else "Unknown"
                        
                        link_elem = soup.find("a", {"class": "btn btn-primary"})
                        if link_elem and "udemy.com" in link_elem.get("href", ""):
                            courses.append(Course(title, link_elem.get("href")))
                except Exception as e:
                    log.debug(f"Error parsing E-Next course: {e}")
            
            log.info(f"E-Next: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"E-Next scraping failed: {e}")
            return courses
    
    def scrape_coursejoiner(self) -> list:
        """Scrape CourseJoiner.com via WP-JSON API"""
        courses = []
        try:
            log.info("Scraping CourseJoiner...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self._fetch_page,
                        f"https://www.coursejoiner.com/wp-json/wp/v2/posts?categories=74&per_page=100&page={page}"
                    )
                    for page in range(1, 3)
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    resp = future.result()
                    if not resp or resp.status_code != 200:
                        continue
                    
                    data = resp.json()
                    if not data:
                        break
                    
                    for item in data:
                        try:
                            title = unescape(item.get("title", {}).get("rendered", "Unknown"))
                            title = title.replace("–", "-").strip().removesuffix("- (Free Course)").strip()
                            
                            content = item.get("content", {}).get("rendered", "")
                            soup = self._parse_html(content)
                            link_elem = soup.find("a", string="APPLY HERE")
                            
                            if link_elem and link_elem.has_attr("href"):
                                link = link_elem.get("href", "")
                                if "udemy.com" in link:
                                    courses.append(Course(title, link))
                        except Exception as e:
                            log.debug(f"Error parsing CourseJoiner course: {e}")
            
            log.info(f"CourseJoiner: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"CourseJoiner scraping failed: {e}")
            return courses
    
    def scrape_courson(self) -> list:
        """Scrape Courson.xyz via POST API"""
        courses = []
        try:
            log.info("Scraping Courson...")
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        lambda p: requests.post(
                            "https://courson.xyz/load-more-coupons",
                            json={"filters": {}, "offset": (p - 1) * 30},
                            timeout=15
                        ) if CURL_CFFI_AVAILABLE else requests.post(
                            "https://courson.xyz/load-more-coupons",
                            json={"filters": {}, "offset": (p - 1) * 30},
                            timeout=15
                        ),
                        page
                    )
                    for page in range(1, 6)
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    try:
                        resp = future.result()
                        if resp and resp.status_code == 200:
                            data = resp.json().get("coupons", [])
                            if not data:
                                continue
                            
                            for item in data:
                                title = item.get("headline", "").strip(' "')
                                coupon_code = item.get("coupon_code", "")
                                link = f"https://www.udemy.com/course/{item.get('id_name', '')}/?" \
                                       f"couponCode={coupon_code}"
                                courses.append(Course(title, link, coupon_code=coupon_code))
                    except Exception as e:
                        log.debug(f"Error parsing Courson response: {e}")
            
            log.info(f"Courson: Found {len(courses)} courses")
            return courses
        except Exception as e:
            log.error(f"Courson scraping failed: {e}")
            return courses
    
    def scrape_all(self, sites: list = None) -> dict:
        """Scrape all enabled sites and return dict with valid (non-expired) courses"""
        if sites is None:
            sites = ["discudemy", "udemyfreebies", "tutorialbar", "realdiscount", "coursevania", "enext", "coursejoiner", "courson"]
        
        all_courses = {site: [] for site in sites}
        
        scrapers = {
            "discudemy": self.scrape_discudemy,
            "udemyfreebies": self.scrape_udemyfreebies,
            "tutorialbar": self.scrape_tutorialbar,
            "realdiscount": self.scrape_realdiscount,
            "coursevania": self.scrape_coursevania,
            "enext": self.scrape_enext,
            "coursejoiner": self.scrape_coursejoiner,
            "courson": self.scrape_courson,
        }
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = {executor.submit(scrapers[site]): site for site in sites if site in scrapers}
            for future in concurrent.futures.as_completed(futures):
                site = futures[future]
                try:
                    courses = future.result()
                    valid_courses = [c for c in courses if c.is_valid()]
                    all_courses[site] = valid_courses
                except Exception as e:
                    log.error(f"Error scraping {site}: {e}")
        
        return all_courses


class UdemyAutoEnroller:
    """
    Enrolls user in Udemy courses using their access_token and client_id.
    Based on the techtanic/Discounted-Udemy-Course-Enroller approach:
    - Uses proper session with cookies
    - Gets CSRF token for checkout
    - Uses bulk checkout with correct payload format
    - Handles free courses separately via /course/subscribe/ endpoint
    """
    
    def __init__(self, access_token: str, client_id: str):
        self.access_token = access_token
        self.client_id = client_id
        self.cookie_dict = {
            "access_token": access_token,
            "client_id": client_id,
        }
        self.session = requests.Session()
        self.session.cookies.update(self.cookie_dict)
        self.session.headers.update({
            "User-Agent": "okhttp/4.10.0 UdemyAndroid 9.7.0(515) (phone)",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US",
            "X-Requested-With": "XMLHttpRequest",
            "DNT": "1",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Referer": "https://www.udemy.com/",
        })
        self.currency = "inr"
        self.enrolled_slugs = set()
        self.enrolled = []
        self.already_enrolled = []
        self.failed = []
        self.expired = []
    
    def _get(self, url: str, **kwargs) -> requests.Response:
        """Session GET with retry"""
        for _ in range(3):
            try:
                r = self.session.get(url, timeout=15, **kwargs)
                return r
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                log.debug(f"GET failed for {url}: {e}")
                return None
        return None
    
    def _post(self, url: str, **kwargs) -> requests.Response:
        """Session POST with retry"""
        for _ in range(3):
            try:
                r = self.session.post(url, timeout=20, **kwargs)
                return r
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                log.debug(f"POST failed for {url}: {e}")
                return None
        return None
    
    def verify_login(self) -> bool:
        """Verify the session is valid by checking /contexts/me/"""
        try:
            r = self._get("https://www.udemy.com/api-2.0/contexts/me/?header=True")
            if r and r.status_code == 200:
                data = r.json()
                if data.get("header", {}).get("isLoggedIn"):
                    name = data["header"]["user"].get("display_name", "User")
                    log.info(f"Logged in as: {name}")
                    # Get currency
                    r2 = self._get("https://www.udemy.com/api-2.0/shopping-carts/me/")
                    if r2 and r2.status_code == 200:
                        cart = r2.json()
                        self.currency = cart.get("user", {}).get("credit", {}).get("currency_code", "inr")
                    return True
            log.error("Login verification failed")
            return False
        except Exception as e:
            log.error(f"Login check error: {e}")
            return False
    
    def _get_enrolled_courses(self):
        """Pre-fetch enrolled courses to avoid redundant API calls"""
        next_page = "https://www.udemy.com/api-2.0/users/me/subscribed-courses/?ordering=-enroll_time&fields[course]=enrollment_time,url&page_size=100"
        while next_page:
            r = self._get(next_page)
            if not r or r.status_code != 200:
                break
            try:
                data = r.json()
            except Exception:
                break
            for course in data.get("results", []):
                url_parts = course.get("url", "").split("/")
                slug = url_parts[2] if len(url_parts) > 2 else None
                if slug == "draft" and len(url_parts) > 3:
                    slug = url_parts[3]
                if slug:
                    self.enrolled_slugs.add(slug)
            next_page = data.get("next")
    
    @staticmethod
    def _extract_slug(url: str) -> str:
        """Extract course slug from Udemy URL"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            parts = parsed.path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "course":
                return parts[1]
            elif len(parts) >= 1:
                return parts[0]
        except Exception:
            pass
        return None
    
    @staticmethod
    def _extract_coupon(url: str) -> str:
        """Extract coupon code from URL query params"""
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            codes = params.get("couponCode", [None])
            return codes[0] if codes else None
        except Exception:
            return None
    
    def _get_course_id_from_page(self, slug: str) -> tuple:
        """Get course_id and is_free by fetching the course page HTML"""
        url = f"https://www.udemy.com/course/{slug}/"
        r = self._get(url)
        if not r or r.status_code != 200:
            return None, False
        try:
            soup = BeautifulSoup(r.content, "html.parser")
            body = soup.find("body")
            if not body:
                return None, False
            course_id = body.get("data-clp-course-id")
            if course_id and course_id != "invalid":
                import json as _json
                is_free = False
                dma_str = body.get("data-module-args")
                if dma_str:
                    try:
                        dma = _json.loads(dma_str)
                        is_free = not dma.get("serverSideProps", {}).get("course", {}).get("isPaid", True)
                    except Exception:
                        pass
                return str(course_id), is_free
        except Exception as e:
            log.debug(f"Failed to parse course page for {slug}: {e}")
        return None, False
    
    def _check_coupon(self, course_id: str, coupon: str) -> bool:
        """Check if coupon gives 100% discount. Returns True if valid and free."""
        url = (
            f"https://www.udemy.com/api-2.0/course-landing-components/{course_id}/me/"
            f"?components=purchase,redeem_coupon&couponCode={coupon}"
        )
        r = self._get(url)
        if not r or r.status_code != 200:
            return False
        try:
            data = r.json()
            if "redeem_coupon" in data:
                attempts = data["redeem_coupon"].get("discount_attempts", [])
                if attempts and attempts[0].get("status") == "applied":
                    discount = data.get("purchase", {}).get("data", {}).get("pricing_result", {}).get("discount_percent", 0)
                    return discount == 100
            # Fallback: check if price is 0
            amount = (
                data.get("purchase", {})
                .get("data", {})
                .get("pricing_result", {})
                .get("price", {})
                .get("amount", -1)
            )
            return amount == 0
        except Exception:
            return False
    
    def _free_checkout(self, course_id: str) -> bool:
        """Enroll in a naturally free course (no coupon needed)"""
        self._get(f"https://www.udemy.com/course/subscribe/?courseId={course_id}")
        r = self._get(
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/"
            f"?fields%5Bcourse%5D=%40default%2Cbuyable_object_type%2Cprimary_subcategory%2Cis_private"
        )
        if r and r.status_code == 200:
            return True
        return False
    
    def _ensure_csrf(self):
        """Ensure we have a CSRF token by visiting checkout page if needed"""
        csrf = self.session.cookies.get("csrftoken", default="")
        if not csrf:
            self._get("https://www.udemy.com/payment/checkout/")
            csrf = self.session.cookies.get("csrftoken", default="")
        return csrf
    
    def _checkout_single(self, course_id: str, coupon_code: str) -> str:
        """
        Enroll in a single paid course via checkout API.
        Returns: "enrolled", "already", or "failed"
        """
        import time as _time
        
        csrf = self._ensure_csrf()
        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {
                "method_id": "0",
                "payment_method": "free-method",
                "payment_vendor": "Free",
            },
            "shopping_info": {
                "items": [{
                    "buyable": {"id": str(course_id), "type": "course"},
                    "discountInfo": {"code": coupon_code},
                    "price": {"amount": 0, "currency": self.currency.upper()},
                }],
                "is_cart": True,
            },
        }
        checkout_headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "x-checkout-is-mobile-app": "false",
            "X-CSRF-Token": csrf,
        }
        
        for attempt in range(2):
            r = self._post(
                "https://www.udemy.com/payment/checkout-submit/",
                json=payload,
                headers=checkout_headers,
            )
            if not r:
                continue
            if r.status_code == 504:
                return "enrolled"
            try:
                data = r.json()
                if data.get("status") == "succeeded":
                    return "enrolled"
            except Exception:
                pass
            _time.sleep(2)
        
        # Check if already enrolled (race condition)
        check = self._get(
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/"
        )
        if check and check.status_code == 200:
            return "already"
        
        return "failed"
    
    def _bulk_checkout(self, courses_to_enroll: list) -> list:
        """
        Bulk enroll using checkout-submit endpoint.
        courses_to_enroll: list of (course_id, coupon_code, title) tuples
        Falls back to one-by-one if batch fails.
        Returns list of titles that were successfully enrolled.
        """
        import time as _time
        
        if not courses_to_enroll:
            return []
        
        csrf = self._ensure_csrf()
        
        items = []
        for course_id, coupon_code, title in courses_to_enroll:
            items.append({
                "buyable": {"id": str(course_id), "type": "course"},
                "discountInfo": {"code": coupon_code} if coupon_code else {},
                "price": {"amount": 0, "currency": self.currency.upper()},
            })
        
        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {
                "method_id": "0",
                "payment_method": "free-method",
                "payment_vendor": "Free",
            },
            "shopping_info": {"items": items, "is_cart": True},
        }
        
        checkout_headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "x-checkout-is-mobile-app": "false",
            "X-CSRF-Token": csrf,
        }
        
        # Try bulk first
        for attempt in range(2):
            r = self._post(
                "https://www.udemy.com/payment/checkout-submit/",
                json=payload,
                headers=checkout_headers,
            )
            if not r:
                continue
            
            if r.status_code == 504:
                return [t for _, _, t in courses_to_enroll]
            
            try:
                resp_data = r.json()
            except Exception:
                continue
            
            if resp_data.get("status") == "succeeded":
                return [t for _, _, t in courses_to_enroll]
            
            log.debug(f"Bulk checkout attempt {attempt+1} failed: {resp_data}")
            self._get("https://www.udemy.com/payment/checkout/")
            _time.sleep(3 + attempt)
        
        # Bulk failed - fall back to one-by-one
        log.info("Bulk checkout failed, trying one-by-one...")
        enrolled_titles = []
        for course_id, coupon_code, title in courses_to_enroll:
            result = self._checkout_single(course_id, coupon_code)
            if result in ("enrolled", "already"):
                enrolled_titles.append(title)
            _time.sleep(1)
        
        return enrolled_titles
    
    def enroll_in_courses(self, courses: list, progress_callback=None) -> dict:
        """
        Enroll in a list of Course objects.
        Returns dict with results: enrolled, already_enrolled, failed, expired
        """
        self.enrolled = []
        self.already_enrolled = []
        self.failed = []
        self.expired = []
        
        total = len(courses)
        if total == 0:
            return {"enrolled": [], "already_enrolled": [], "failed": [], "expired": [], "total": 0}
        
        # Verify login first
        if not self.verify_login():
            return {
                "enrolled": [],
                "already_enrolled": [],
                "failed": [{"title": "ALL", "reason": "Login failed - check access_token/client_id"}],
                "expired": [],
                "total": total,
            }
        
        # Pre-fetch enrolled courses for fast duplicate check
        self._get_enrolled_courses()
        
        # Batch valid courses for bulk checkout
        batch = []
        
        for i, course in enumerate(courses):
            if progress_callback and i % 3 == 0:
                progress_callback(i, total)
            
            try:
                slug = self._extract_slug(course.url)
                if not slug:
                    self.failed.append({"title": course.title, "reason": "Invalid URL"})
                    continue
                
                # Quick check if already enrolled
                if slug in self.enrolled_slugs:
                    self.already_enrolled.append(course.title)
                    continue
                
                coupon = course.coupon_code or self._extract_coupon(course.url)
                
                # Get course ID from page
                course_id, is_free = self._get_course_id_from_page(slug)
                if not course_id:
                    self.failed.append({"title": course.title, "reason": "Course not found/ID missing"})
                    continue
                
                # Free course (no coupon needed)
                if is_free:
                    if self._free_checkout(course_id):
                        self.enrolled.append(course.title)
                    else:
                        self.failed.append({"title": course.title, "reason": "Free enrollment failed"})
                    continue
                
                # Paid course with coupon - validate coupon
                if not coupon:
                    self.failed.append({"title": course.title, "reason": "No coupon code"})
                    continue
                
                if not self._check_coupon(course_id, coupon):
                    self.expired.append({"title": course.title, "reason": "Coupon expired/not 100% off"})
                    continue
                
                # Add to batch for bulk checkout
                batch.append((course_id, coupon, course.title))
                
                # Bulk checkout every 5 courses
                if len(batch) >= 5:
                    enrolled_titles = self._bulk_checkout(batch)
                    self.enrolled.extend(enrolled_titles)
                    not_enrolled = [t for _, _, t in batch if t not in enrolled_titles]
                    for t in not_enrolled:
                        self.failed.append({"title": t, "reason": "Bulk checkout failed"})
                    batch.clear()
                    
            except Exception as e:
                self.failed.append({"title": course.title, "reason": str(e)[:50]})
        
        # Final batch
        if batch:
            enrolled_titles = self._bulk_checkout(batch)
            self.enrolled.extend(enrolled_titles)
            not_enrolled = [t for _, _, t in batch if t not in enrolled_titles]
            for t in not_enrolled:
                self.failed.append({"title": t, "reason": "Bulk checkout failed"})
        
        if progress_callback:
            progress_callback(total, total)
        
        return {
            "enrolled": self.enrolled,
            "already_enrolled": self.already_enrolled,
            "failed": self.failed,
            "expired": self.expired,
            "total": total,
        }
