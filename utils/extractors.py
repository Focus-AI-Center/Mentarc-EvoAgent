"""Structured data extractors for SSE code 11 panel rendering.

Extract structured JSON from agent responses or user input using regex.
Used by the API layer (app/api/routers/agent.py) to emit SSE code 11.
"""

import re


def extract_inquiry_analysis(text: str) -> dict | None:
    """Extract inquiry analysis fields for frontend panel.

    Returns: {type, subject, buyer_info, core_concerns} or None.
    """
    # Trigger: detect 评估小结 heading
    if not re.search(r"##\s*📊?\s*评估小结", text):
        return None

    # 询盘主题
    subject = ""
    for pattern in [
        r"\*\*询盘主题\*\*[：:]\s*(.+)",
        r"\*\*邮件主题\*\*[：:]\s*(.+)",
        r"\*\*Subject\*\*[：:]\s*(.+)",
    ]:
        m = re.search(pattern, text)
        if m:
            subject = re.sub(r"\*{1,2}", "", m.group(1)).strip()
            break

    # 买家信息 (keep as single string)
    buyer_info = ""
    bm = re.search(r"\*\*买家(?:背景|信息)\*\*[：:]\s*(.+)", text)
    if bm:
        buyer_info = re.sub(r"\*{1,2}", "", bm.group(1)).strip()

    # 核心关注点
    core_concerns = ""
    fm = re.search(r"\*\*核心关注点\*\*[：:]\s*(.+)", text)
    if fm:
        core_concerns = re.sub(r"\*{1,2}", "", fm.group(1)).strip()

    return {
        "type": "inquiry_analysis",
        "subject": subject,
        "buyer_info": buyer_info,
        "core_concerns": core_concerns,
    }


# Patterns to strip from product info text (at least one action verb required)
_INSTRUCTION_PATTERNS = re.compile(
    r"(帮我|请|麻烦|帮忙)?"
    r"(进行|做|对|给我|用这个|按照?这个?)?"
    r"(竞品|产品|listing|文案|描述|标题|内容|风格|图片|主图)?"
    r"(的|和)?"
    r"(复刻|生成|写|优化|分析|对比|调研|仿写|洗稿|对标|查询)"
    r"(一下|下)?"
    r"(这个|这款|该)?"
    r"(竞品|产品|listing|文案|描述|标题|内容|风格|图片|主图)?"
    r"(的|和)?"
    r"(复刻|生成|写|优化|分析|对比|调研|仿写|洗稿|对标|查询)?"
    r"[，,。.]?\s*",
)
# Fallback: if cleaned text is only instruction fragment words, discard
_GARBAGE_RE = re.compile(
    r"^(帮我?|请|进行|做|对|给|用|按|竞品|产品|listing|文案|描述|"
    r"标题|内容|风格|图片|主图|复刻|生成|分析|优化|对比|调研|"
    r"仿写|洗稿|对标|查询|链接|参考|一下|这个|这款|该|的|和|了)+$"
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
    product_name = re.sub(
        r"(^|\s)(帮我?|请|进行|做|竞品|产品|listing|文案|描述|标题|内容|"
        r"风格|图片|主图|复刻|生成|分析|优化|对比|调研|仿写|洗稿|对标|查询)+\s*$",
        "",
        product_name,
    ).strip()
    product_name = re.sub(r"^[，,。.\s]+|[，,。.\s]+$", "", product_name)

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
