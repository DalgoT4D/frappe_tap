import os
import re
from urllib.parse import unquote, urlparse

import requests


AUDIO_EXTENSIONS = {"mp3", "wav", "m4a", "aac", "ogg", "opus", "flac"}
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tiff", "heic", "svg"}
VIDEO_EXTENSIONS = {"mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp", "mpeg", "flv", "wmv"}

CONTENT_TYPE_MEDIA_OVERRIDES = {
    "application/ogg": "audio",
}


def detect_url_media_type(url, default="image"):
    """
    Detect whether a URL points to audio, image, or video.

    Extensionless providers like Gupshup expose the actual type only through
    Content-Type, so URL extension detection is only the first, cheap path.
    """
    media_type = _detect_media_type_from_url(url)
    if media_type:
        return media_type

    response = _probe_url(url)
    if response:
        media_type = _detect_media_type_from_response(response)
        _close_response(response)
        if media_type:
            return media_type

    return default


def _detect_media_type_from_url(url):
    path = urlparse(str(url).strip()).path
    return _detect_media_type_from_filename(path)


def _detect_media_type_from_response(response):
    content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type.startswith("audio/"):
        return "audio"
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("video/"):
        return "video"
    if content_type in CONTENT_TYPE_MEDIA_OVERRIDES:
        return CONTENT_TYPE_MEDIA_OVERRIDES[content_type]

    filename = _filename_from_content_disposition(
        response.headers.get("content-disposition", "")
    )
    if filename:
        return _detect_media_type_from_filename(filename)

    return None


def _detect_media_type_from_filename(filename):
    ext = os.path.splitext(unquote(str(filename).strip()))[1].lower().lstrip(".")
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "image"
    return None


def _filename_from_content_disposition(content_disposition):
    if not content_disposition:
        return None

    utf8_match = re.search(
        r"filename\*\s*=\s*UTF-8''([^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )
    if utf8_match:
        return unquote(utf8_match.group(1).strip().strip('"'))

    match = re.search(
        r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;]+)',
        content_disposition,
        flags=re.IGNORECASE,
    )
    if match:
        return (match.group(1) or match.group(2)).strip()

    return None


def _probe_url(url):
    try:
        response = requests.head(url, allow_redirects=True, timeout=(3, 5))
        if response.ok and response.headers.get("content-type"):
            return response
        _close_response(response)
    except requests.exceptions.RequestException:
        pass

    try:
        response = requests.get(url, allow_redirects=True, stream=True, timeout=(3, 5))
        if response.ok:
            return response
        _close_response(response)
    except requests.exceptions.RequestException:
        pass

    return None


def _close_response(response):
    close = getattr(response, "close", None)
    if close:
        close()
