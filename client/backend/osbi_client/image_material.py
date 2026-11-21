"""Body-image enrichment before coarse indexing; no HTML/markdown roundtrip.

Ported from the production wiseflow image_material. Fills image.text on a working
copy of the capture so snapshot_index._image_text splices recognized text into the
body at the recorded DOM offset. Most images never reach the provider: trackers,
tiny/broken images, sufficient alt text and long surrounding paragraphs short-circuit.
"""
import asyncio
import logging
import re
from urllib.parse import urljoin, urlsplit

from .snapshot_index import prepare_capture, image_eligible
from .vision import VisionClient, InvalidImageError, DECORATIVE_MARKER, EMPTY_MARKERS

logger = logging.getLogger(__name__)

VISION_PAGE_BUDGET_SECONDS = 300
_INVALID_IMAGE = object()
_TRACKERS = ("1x1.", "pixel.", "spacer.", "blank.", "tracking", "beacon", "dot.gif", "clear.gif")
_GENERIC_ALT = {"图片", "图", "image", "img", "photo"}
_DECORATION_KEYWORDS = re.compile(
    r"图标|卡通图案|简约插画|渐变色线条|手绘线条|抽象.*(?:背景|剪影)|城市天际线轮廓|(?:花朵|小花|枫叶).*图案|标志，")


def _decorative_text(value):
    # Only discard standalone descriptions explicitly identifying decoration;
    # genuine photo scene descriptions and recognized text continue into the body.
    return value == DECORATIVE_MARKER or bool(
        re.fullmatch(r"[（(][^\n]{0,300}[）)]", value) and _DECORATION_KEYWORDS.search(value))


class ImageEnricher:
    def __init__(self, config, *, cache=None, client=None, limit=None, event=None):
        self.config = config
        self.enabled = config.enabled
        self.cache = cache
        self.client = client or VisionClient(config, limit=limit)
        self.event = event or (lambda *a, **k: None)
        self._tasks = {}

    async def enrich(self, capture):
        result = prepare_capture(capture)
        dom = result.get("dom") or {}
        if result.get("status", {}).get("access") != "ok":
            return result
        base = result.get("finalUrl") or result.get("requestedUrl", "")
        self._tasks = {}
        tasks = []
        for segment in dom.get("segments", []):
            if image_eligible(segment, dom):
                tasks.extend(self._one(image, segment, base) for image in segment.get("images", []))
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks), VISION_PAGE_BUDGET_SECONDS)
            except (TimeoutError, asyncio.TimeoutError):
                # Degrade, never lose the article: mark unfinished images failed.
                for segment in dom.get("segments", []):
                    for image in segment.get("images", []):
                        if image.get("status") == "pending":
                            image.update(text="", source="vision-failed", status="failed")
        return result

    async def _one(self, image, segment, base):
        raw_src = (image.get("dataSrc") or image.get("src") or "").strip()
        image.update({"url": "", "text": "", "source": "", "status": "skipped"})
        if not raw_src:
            # <img src=""> resolves to the page URL if joined; never send the article itself.
            image.update(source="unavailable-image", status="skipped")
            return
        url = urljoin(base, raw_src)
        image["url"] = url
        alt = (image.get("alt") or "").strip()
        if alt.lower() in _GENERIC_ALT:
            alt = ""
        is_link = any(any(x.get("src") and x["src"] in {image.get("src"), image.get("dataSrc")}
                          for x in anchor.get("images", [])) for anchor in segment.get("links", []))
        if image.get("complete") and not (image.get("width") and image.get("height")):
            image.update(source="unavailable-image", status="skipped")
            return
        if (any(x in urlsplit(url).path.lower() for x in _TRACKERS) or
                (image.get("complete") and image.get("width", 0) and
                 min(image.get("width", 0), image.get("height", 0)) < 28)):
            return
        if len(alt) > (2 if is_link else 5):
            image.update(text=alt, source="alt", status="ok")
            return
        if not is_link and len(segment.get("text", "").strip()) > 150:
            image.update(text=alt, source="surrounding-text", status="covered")
            return
        if urlsplit(url).scheme not in {"http", "https", "data"}:
            image.update(status="pending", source="unavailable-image")
            return
        image.update(status="pending", source="vision")
        task = self._tasks.get(url)
        if task is None:
            task = self._tasks[url] = asyncio.create_task(self._recognize(url))
        value = await asyncio.shield(task)
        if value is _INVALID_IMAGE:
            image.update(text="", source="vision-invalid", status="skipped")
            return
        if value is None:
            image.update(text="", source="vision-failed", status="failed")
            return
        if _decorative_text(value):
            image.update(text="", source="vision-decoration", status="skipped")
            return
        value = value.replace(DECORATIVE_MARKER, "").strip()
        image.update(text=value, source="vision", status="ok" if value else "pending")

    async def _recognize(self, url):
        if self.cache:
            cached = self.cache.image_text_get(url)
            if cached is not None and cached not in EMPTY_MARKERS:
                return cached
        try:
            value = await self.client.recognize(url)
        except InvalidImageError as error:
            self.event("image_invalid", f"图片被识别服务拒绝：{str(error)[:200]}", level="warning",
                       details={"url": url, "status": error.status, "code": error.code})
            return _INVALID_IMAGE
        except Exception as error:
            logger.warning("vision recognize failed for %s: %s", url, error)
            return None
        if value and value not in EMPTY_MARKERS and self.cache:
            self.cache.image_text_put(url, value)
        return value
