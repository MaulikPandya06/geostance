import hashlib
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

# Query parameters added by trackers/social media — strip before hashing
_UTM_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "msclkid", "mc_eid", "ref", "source",
    "_ga", "igshid", "twclid",
}


def normalize_url(url: str) -> str:
    """
    Canonicalize a URL so that the same article reached via different
    paths always produces the same string (and therefore the same MD5).

    Rules applied:
      1. Lowercase scheme and netloc
      2. Strip 'www.' prefix
      3. Force https (http → https)
      4. Remove trailing slash from path
      5. Strip known tracking query params (UTM, fbclid, …)
      6. Sort remaining query params for consistency
    """
    url = url.strip()
    try:
        parsed = urlparse(url)
    except Exception:
        return url.lower().strip("/")

    scheme   = "https"                              # normalise http → https
    netloc   = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]                         # strip www.

    path     = parsed.path.rstrip("/") or "/"      # remove trailing slash

    # Filter tracking params, sort survivors
    clean_qs = urlencode(sorted(
        [(k, v) for k, v in parse_qsl(parsed.query)
         if k.lower() not in _UTM_PARAMS]
    ))

    normalised = urlunparse((scheme, netloc, path, "", clean_qs, ""))
    return normalised


def url_to_post_id(url: str) -> str:
    """Return a stable MD5 hex digest of the normalised URL."""
    return hashlib.md5(normalize_url(url).encode()).hexdigest()


def normalize_text(text):

    text = text.lower()

    text = re.sub(r'[^\w\s]', '', text)

    text = re.sub(r'\s+', ' ', text)

    return text.strip()


def similarity(a, b):

    return SequenceMatcher(
        None,
        normalize_text(a),
        normalize_text(b)
    ).ratio()
