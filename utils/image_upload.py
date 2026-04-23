"""Save generated images to local workspace and return serving URL."""

import uuid
from pathlib import Path

from configs.settings import WORKSPACE_ROOT
from utils.logger import logger


def upload_image(image_data: bytes, ext: str = "png", user_id: str = "default") -> str:
    """Save image to local workspace and return a URL for access.

    Args:
        image_data: Raw image bytes
        ext: File extension (png, jpg, webp)
        user_id: User identifier for workspace directory

    Returns:
        URL string like /api/workspace/{user_id}/raw/images/{filename}
    """
    filename = f"{uuid.uuid4().hex[:12]}.{ext}"
    images_dir = Path(WORKSPACE_ROOT) / user_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    path = images_dir / filename
    path.write_bytes(image_data)
    logger.info(f"Image saved: {path} ({len(image_data)} bytes)")

    return f"/api/workspace/{user_id}/raw/images/{filename}"
