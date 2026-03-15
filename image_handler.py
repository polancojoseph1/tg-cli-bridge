import base64
import logging
import os
import tempfile

import httpx

from config import GEMINI_API_KEY

logger = logging.getLogger("bridge.image")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
IMAGE_MODEL = "gemini-2.5-flash-image"

if not GEMINI_API_KEY:
    logger.warning("GEMINI_API_KEY is not set — image generation will be unavailable")


async def generate_image(prompt: str) -> tuple[str, str]:
    """Generate an image using Gemini's image generation.

    Returns (image_path, description) where image_path is a temp PNG file
    and description is any text the model returned alongside the image.
    Raises on failure.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")

    url = f"{GEMINI_API_BASE}/models/{IMAGE_MODEL}:generateContent"
    headers = {"x-goog-api-key": GEMINI_API_KEY}

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
        },
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, json=payload, headers=headers)

    if resp.status_code != 200:
        error_msg = resp.text[:500]
        logger.error("Gemini API error %d: %s", resp.status_code, error_msg)
        raise RuntimeError(f"Gemini API error {resp.status_code}: {error_msg}")

    data = resp.json()

    # Extract image and text from response
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("No candidates in Gemini response")

    parts = candidates[0].get("content", {}).get("parts", [])
    image_path = None
    description = ""

    for part in parts:
        if "inlineData" in part:
            # Decode base64 image
            inline = part["inlineData"]
            mime_type = inline.get("mimeType", "image/png")
            ext = ".png" if "png" in mime_type else ".jpg"
            image_bytes = base64.b64decode(inline["data"])

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, prefix="gemini_img_")
            tmp.write(image_bytes)
            tmp.close()
            os.chmod(tmp.name, 0o600)
            image_path = tmp.name
            logger.info("Generated image saved to %s (%d bytes)", image_path, len(image_bytes))

        elif "text" in part:
            description = part["text"]

    if not image_path:
        raise RuntimeError("No image returned by Gemini. Response: " + str(data)[:300])

    return image_path, description
