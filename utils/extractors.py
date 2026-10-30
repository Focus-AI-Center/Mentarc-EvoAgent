"""Structured data extractors for SSE code 11 panel rendering.

Extract structured JSON from agent responses or user input using regex.
Used by the API layer (app/api/routers/agent.py) to emit SSE code 11.
"""

import re


def extract_inquiry_analysis(text: str) -> dict | None:
    """Extract inquiry analysis fields for frontend panel.

    Returns: {type, subject, buyer_info, core_concerns} or None.
    """
    if not re.search(r"##\s*📊?\s*Assessment summary", text, re.I):
        return None

    def field(pattern: str) -> str:
        match = re.search(pattern, text, re.I)
        return re.sub(r"\*{1,2}", "", match.group(1)).strip() if match else ""

    subject = field(r"\*\*(?:Inquiry subject|Email subject|Subject)\*\*:\s*(.+)")
    buyer_info = field(r"\*\*Buyer (?:background|information)\*\*:\s*(.+)")
    core_concerns = field(r"\*\*Core concerns\*\*:\s*(.+)")

    return {
        "type": "inquiry_analysis",
        "subject": subject,
        "buyer_info": buyer_info,
        "core_concerns": core_concerns,
    }


# Patterns to strip from product info text (at least one action verb required)
_INSTRUCTION_PATTERNS = re.compile(
    r"^(?:(?:please|help me|could you|can you)\s+)*"
    r"(?:replicate|generate|write|optimize|analyze|compare|research|rewrite|benchmark|look up)\b\s*"
    r"(?:(?:this|that|the|a)\s+)?"
    r"(?:(?:competitor|product|listing|copy|description|title|content|style|image)\b\s*)?",
    re.I,
)
_GARBAGE_RE = re.compile(
    r"^(?:(?:please|help|me|this|that|the|a|product|listing|image|link|reference|and|for|of)\b[\s,.]*)+$",
    re.I,
)
_URL_RE = re.compile(r"https?://\S+")
_IMAGE_URL_RE = re.compile(r"https?://\S+\.(?:jpg|jpeg|png|webp|gif)(?:\?\S*)?", re.I)


def extract_product_replication(user_input: str, user_profile: dict) -> dict | None:
    """Extract product materials for frontend panel.

    Returns: {type, product_name, competitor_urls, my_product_info, product_images} or None.
    """
    # Extract URLs
    urls = _URL_RE.findall(user_input)
    if not urls:
        return None

    # Extract product name: remove URLs and instruction phrases
    product_name = _URL_RE.sub("", user_input).strip()
    product_name = _INSTRUCTION_PATTERNS.sub("", product_name).strip()
    product_name = product_name.strip(" ,.")

    if len(product_name) < 4 or _GARBAGE_RE.match(product_name):
        product_name = ""

    # My product info from user profile
    my_product_info = user_profile.get("main_products", "").strip()

    # Product images: image URLs from user input
    product_images = _IMAGE_URL_RE.findall(user_input)

    return {
        "type": "product_replication",
        "product_name": product_name,
        "competitor_urls": urls,
        "my_product_info": my_product_info,
        "product_images": product_images,
    }


# Extractor registry: skill_name → extractor function
# "input" extractors run at stream_start on user input
# "output" extractors run at stream_end on agent response
INPUT_EXTRACTORS = {
    "ecommerce-product-replication": extract_product_replication,
}
OUTPUT_EXTRACTORS = {
    "inquiry-reply": extract_inquiry_analysis,
}
