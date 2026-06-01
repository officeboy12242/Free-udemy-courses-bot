"""
Udemy Course Auto-Enroller
- Course class for representing Udemy courses
- UdemyAutoEnroller for enrolling via Udemy's checkout API
"""

import logging
import requests
from datetime import datetime
from bs4 import BeautifulSoup

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
        if not self.expires_at:
            return
        try:
            exp_date = datetime.fromisoformat(self.expires_at.replace('Z', '+00:00'))
            self.is_expired = datetime.now(exp_date.tzinfo) > exp_date
        except Exception:
            pass
    
    def is_valid(self) -> bool:
        return not self.is_expired and "udemy.com" in self.url
    
    def __repr__(self):
        return f"Course({self.title[:30]})"


class UdemyAutoEnroller:
    """
    Enrolls user in Udemy courses using access_token and client_id cookies.
    Uses bulk checkout with one-by-one fallback.
    """
    
    def __init__(self, access_token: str, client_id: str):
        self.access_token = access_token
        self.client_id = client_id
        self.session = requests.Session()
        self.session.cookies.update({
            "access_token": access_token,
            "client_id": client_id,
        })
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
    
    def _get(self, url: str, **kwargs) -> requests.Response:
        for _ in range(3):
            try:
                return self.session.get(url, timeout=15, **kwargs)
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                log.debug(f"GET failed: {e}")
                return None
        return None
    
    def _post(self, url: str, **kwargs) -> requests.Response:
        for _ in range(3):
            try:
                return self.session.post(url, timeout=20, **kwargs)
            except requests.exceptions.ConnectionError:
                continue
            except Exception as e:
                log.debug(f"POST failed: {e}")
                return None
        return None
    
    def verify_login(self) -> bool:
        """Verify session is valid"""
        try:
            r = self._get("https://www.udemy.com/api-2.0/contexts/me/?header=True")
            if r and r.status_code == 200:
                data = r.json()
                if data.get("header", {}).get("isLoggedIn"):
                    r2 = self._get("https://www.udemy.com/api-2.0/shopping-carts/me/")
                    if r2 and r2.status_code == 200:
                        self.currency = r2.json().get("user", {}).get("credit", {}).get("currency_code", "inr")
                    return True
            return False
        except Exception:
            return False
    
    def _get_enrolled_courses(self):
        """Pre-fetch enrolled course slugs"""
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
    
    def get_total_courses_count(self) -> int:
        """Get total number of courses in the Udemy account"""
        try:
            r = self._get("https://www.udemy.com/api-2.0/users/me/subscribed-courses/?page_size=1")
            if r and r.status_code == 200:
                data = r.json()
                return data.get("count", 0)
            return -1  # Error
        except Exception as e:
            log.debug(f"Failed to get course count: {e}")
            return -1
    
    @staticmethod
    def _extract_slug(url: str) -> str:
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
        try:
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(url)
            params = parse_qs(parsed.query)
            codes = params.get("couponCode", [None])
            return codes[0] if codes else None
        except Exception:
            return None
    
    def _get_course_id_from_page(self, slug: str) -> tuple:
        """Get (course_id, is_free) from course page HTML"""
        r = self._get(f"https://www.udemy.com/course/{slug}/")
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
            log.debug(f"Parse error for {slug}: {e}")
        return None, False
    
    def _check_coupon(self, course_id: str, coupon: str) -> bool:
        """Check if coupon gives 100% discount"""
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
            amount = (
                data.get("purchase", {}).get("data", {}).get("pricing_result", {}).get("price", {}).get("amount", -1)
            )
            return amount == 0
        except Exception:
            return False
    
    def _free_checkout(self, course_id: str) -> str:
        """Enroll in a naturally free course. Returns 'enrolled', 'already', or 'failed'."""
        # Check if already enrolled first
        check = self._get(f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/")
        if check and check.status_code == 200:
            return "already"
        
        self._get(f"https://www.udemy.com/course/subscribe/?courseId={course_id}")
        r = self._get(
            f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/"
            f"?fields%5Bcourse%5D=%40default%2Cbuyable_object_type%2Cprimary_subcategory%2Cis_private"
        )
        if r and r.status_code == 200:
            return "enrolled"
        return "failed"
    
    def _ensure_csrf(self):
        csrf = self.session.cookies.get("csrftoken", default="")
        if not csrf:
            self._get("https://www.udemy.com/payment/checkout/")
            csrf = self.session.cookies.get("csrftoken", default="")
        return csrf
    
    def _checkout_single(self, course_id: str, coupon_code: str, was_enrolled_before: bool = False) -> str:
        """Enroll single course. Returns 'enrolled', 'already', or 'failed'."""
        import time as _time
        
        csrf = self._ensure_csrf()
        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {"method_id": "0", "payment_method": "free-method", "payment_vendor": "Free"},
            "shopping_info": {
                "items": [{
                    "buyable": {"id": str(course_id), "type": "course"},
                    "discountInfo": {"code": coupon_code},
                    "price": {"amount": 0, "currency": self.currency.upper()},
                }],
                "is_cart": True,
            },
        }
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "x-checkout-is-mobile-app": "false",
            "X-CSRF-Token": csrf,
        }
        
        for _ in range(2):
            r = self._post("https://www.udemy.com/payment/checkout-submit/", json=payload, headers=headers)
            if not r:
                continue
            if r.status_code == 504:
                return "enrolled"
            try:
                if r.json().get("status") == "succeeded":
                    return "enrolled"
            except Exception:
                pass
            _time.sleep(2)
        
        # Check if now subscribed - if we weren't before, this means checkout worked
        check = self._get(f"https://www.udemy.com/api-2.0/users/me/subscribed-courses/{course_id}/")
        if check and check.status_code == 200:
            # If wasn't enrolled before checkout attempt, it means we enrolled it now
            return "enrolled" if not was_enrolled_before else "already"
        return "failed"
    
    def _bulk_checkout(self, courses_to_enroll: list) -> list:
        """
        Bulk enroll via checkout-submit. Falls back to one-by-one.
        courses_to_enroll: list of (course_id, coupon_code, title)
        Returns list of enrolled titles.
        """
        import time as _time
        
        if not courses_to_enroll:
            return []
        
        csrf = self._ensure_csrf()
        items = [
            {
                "buyable": {"id": str(cid), "type": "course"},
                "discountInfo": {"code": coup} if coup else {},
                "price": {"amount": 0, "currency": self.currency.upper()},
            }
            for cid, coup, _ in courses_to_enroll
        ]
        
        payload = {
            "checkout_environment": "Marketplace",
            "checkout_event": "Submit",
            "payment_info": {"method_id": "0", "payment_method": "free-method", "payment_vendor": "Free"},
            "shopping_info": {"items": items, "is_cart": True},
        }
        headers = {
            "Content-Type": "application/json",
            "Referer": "https://www.udemy.com/payment/checkout/",
            "Origin": "https://www.udemy.com",
            "Host": "www.udemy.com",
            "x-checkout-is-mobile-app": "false",
            "X-CSRF-Token": csrf,
        }
        
        for attempt in range(2):
            r = self._post("https://www.udemy.com/payment/checkout-submit/", json=payload, headers=headers)
            if not r:
                continue
            if r.status_code == 504:
                return [t for _, _, t in courses_to_enroll]
            try:
                if r.json().get("status") == "succeeded":
                    return [t for _, _, t in courses_to_enroll]
            except Exception:
                pass
            self._get("https://www.udemy.com/payment/checkout/")
            _time.sleep(3 + attempt)
        
        # Fallback: one-by-one (we know these weren't enrolled before)
        enrolled = []
        for cid, coup, title in courses_to_enroll:
            result = self._checkout_single(cid, coup, was_enrolled_before=False)
            if result == "enrolled":
                enrolled.append(title)
            _time.sleep(1)
        return enrolled
