import os

from dotenv import load_dotenv

load_dotenv()

# --- LLM ---
MODEL = os.getenv("MODEL", "gpt-5.2")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_API_BASE = os.getenv("OPENAI_API_BASE") or "https://api.openai.com/v1"
OPENAI_API_BASE_SECONDARY = os.getenv("OPENAI_API_BASE_SECONDARY") or OPENAI_API_BASE
MODEL_VISION = os.getenv("MODEL_VISION", "gpt-5.4-mini")
OPENAI_API_BASE_VISION = os.getenv("OPENAI_API_BASE_VISION") or OPENAI_API_BASE
PROXY_SERVER = os.getenv("PROXY_SERVER")  # e.g. https://proxy.example.com

# --- Server ---
SERVER_BASE_URL = os.getenv("SERVER_BASE_URL", "http://localhost:8194")

# --- Workspace ---
DEFAULT_PROMPTS_DIR = os.getenv("DEFAULT_PROMPTS_DIR", "configs/prompts")
DEFAULT_SKILLS_DIR = os.getenv("DEFAULT_SKILLS_DIR", "configs/skills")
WORKSPACE_ROOT = os.getenv("WORKSPACE_ROOT", "data/workspace")

# --- Agent ---
LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
LLM_RETRY_DELAY = float(os.getenv("LLM_RETRY_DELAY", "2.0"))
ENABLE_METRICS = os.getenv("ENABLE_METRICS", "true").lower() == "true"
MAX_STEPS = int(os.getenv("MAX_STEPS", "100"))
MAX_HISTORY_TOKENS = int(os.getenv("MAX_HISTORY_TOKENS", "128000"))
COMPRESSION_THRESHOLD = float(os.getenv("COMPRESSION_THRESHOLD", "0.5"))
COMPRESSION_LEVEL = os.getenv("COMPRESSION_LEVEL", "moderate")
COMPRESSION_MODEL = os.getenv("COMPRESSION_MODEL", "gpt-5.4-mini")
MIN_MESSAGES_TO_KEEP = int(os.getenv("MIN_MESSAGES_TO_KEEP", "10"))

# --- Plugins ---
# Conservative default: web-only, no code execution, no image tools. Users opt
# into the full set (incl. execute_code) by setting DEFAULT_PLUGINS=all in .env
# — see the comments in .env.example.
DEFAULT_PLUGINS = os.getenv("DEFAULT_PLUGINS", "web_search,fetch_url")

# Tool discovery controls schema visibility, not plugin/MCP authorization.
TOOLSEARCH_ENABLED = os.getenv("TOOLSEARCH_ENABLED", "true").lower() == "true"
TOOLSEARCH_ALWAYS_LOADED = tuple(
    name.strip()
    for name in os.getenv(
        "TOOLSEARCH_ALWAYS_LOADED",
        "memory,skill,update_user_profile,todo_manager,ask_user_question,sub_agent,parent_history",
    ).split(",")
    if name.strip()
)

# --- Skill ---
SKILL_MATCH_EMBEDDING_THRESHOLD = float(os.getenv("SKILL_MATCH_EMBEDDING_THRESHOLD", "0.75"))

# --- Image (Doubao / Volcengine) ---
DOUBAO_API_KEY = os.getenv("DOUBAO_API_KEY", "")
DOUBAO_MODEL = os.getenv("DOUBAO_MODEL", "doubao-seedream-4-5-251128")

# --- Search (Tavily) ---
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
