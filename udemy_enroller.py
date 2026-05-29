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
