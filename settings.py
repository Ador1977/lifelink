"""
Central runtime settings for the LifeLink platform.

This module owns everything that only depends on the environment and the
installed packages (nothing from the app/models/services is imported here),
so it can be imported first by every other module without circular imports.
"""

import os

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

load_dotenv(os.path.join(BASE_DIR, ".env"))

# Where uploaded blood-group card images are kept so the original image stays
# available for later verification (owner/admin can view it via a protected
# route, not through the public static folder).
PROJECT_ROOT = BASE_DIR
BLOOD_GROUP_UPLOAD_DIR = os.path.join(PROJECT_ROOT, "uploads", "blood_groups")
os.makedirs(BLOOD_GROUP_UPLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# VISION MODEL (Ollama + Pillow)
# ---------------------------------------------------------------------------
try:
    import ollama
    from PIL import Image, UnidentifiedImageError
    VLM_AVAILABLE = True
except ImportError:
    ollama = None
    Image = None
    UnidentifiedImageError = None
    VLM_AVAILABLE = False

VLM_MODEL = os.getenv("VLM_MODEL", "qwen2.5vl:7b")

# Text-only chat model used by the FAQ chatbot (separate from the vision
# model above, which is only used for reading uploaded card/report images).
CHAT_MODEL = os.getenv("CHAT_MODEL", "llama3.2")

# Google Maps API key (optional).  When set, hospital geocoding uses the
# Google Geocoding API and pages render real Google Maps embeds; when empty,
# everything falls back to the built-in offline Bangladesh location table.
GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY", "")

# ---------------------------------------------------------------------------
# SHARED CONSTANTS
# ---------------------------------------------------------------------------
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]

URGENCY_ORDER = {"Critical": 0, "High": 1, "Medium": 2, "Low": 3, "Review": 4}

# Registration validation
EMAIL_PATTERN = r'^[^\s@]+@[^\s@]+\.[^\s@]+$'
PHONE_PATTERN = r'^\d{11}$'

ALLOWED_REPORT_EXTENSIONS = {"jpg", "jpeg", "png"}
