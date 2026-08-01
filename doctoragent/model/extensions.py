"""Single source of truth for file extension categorisation."""

# Extensions unlikely to contain sensitive content.
BENIGN_EXTENSIONS: frozenset[str] = frozenset(
    {
        "log",
        "tmp",
        "cache",
        "json",
        "xml",
        "yaml",
        "yml",
        "lock",
        "pyc",
        "pyo",
        "class",
        "jar",
        "exe",
        "dll",
        "so",
        "dylib",
        "woff",
        "woff2",
        "ttf",
        "eot",
        "otf",
    }
)

TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {
        "txt",
        "md",
        "csv",
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "rtf",
        "odt",
        "ods",
        "odp",
        "eml",
        "msg",
    }
)

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {
        "jpg",
        "jpeg",
        "png",
        "gif",
        "bmp",
        "webp",
        "svg",
        "tiff",
        "tif",
        "ico",
        "heic",
        "heif",
    }
)

MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {
        "mp4",
        "mkv",
        "avi",
        "mov",
        "wmv",
        "flv",
        "webm",
        "mp3",
        "wav",
        "flac",
        "aac",
        "ogg",
        "wma",
    }
)

AUDIO_EXTENSIONS: frozenset[str] = frozenset(
    {
        "mp3",
        "wav",
        "m4a",
        "flac",
        "ogg",
    }
)


# Full category map for MIME-type-like lookups.
def mime_type(ext: str) -> str | None:
    ext = ext.lower()
    if ext in TEXT_EXTENSIONS:
        return {
            "txt": "text/plain",
            "md": "text/markdown",
            "csv": "text/csv",
            "json": "application/json",
            "log": "text/plain",
            "xml": "application/xml",
            "html": "text/html",
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "rtf": "application/rtf",
            "eml": "message/rfc822",
            "msg": "application/vnd.ms-outlook",
            "xls": "application/vnd.ms-excel",
            "doc": "application/msword",
            "ppt": "application/vnd.ms-powerpoint",
            "odt": "application/vnd.oasis.opendocument.text",
            "ods": "application/vnd.oasis.opendocument.spreadsheet",
            "odp": "application/vnd.oasis.opendocument.presentation",
        }.get(ext, "text/plain")
    if ext in IMAGE_EXTENSIONS:
        return {
            "png": "image/png",
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "gif": "image/gif",
            "bmp": "image/bmp",
            "tiff": "image/tiff",
            "tif": "image/tiff",
            "webp": "image/webp",
        }.get(ext, "image/unknown")
    if ext in AUDIO_EXTENSIONS:
        return {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "m4a": "audio/mp4",
            "flac": "audio/flac",
            "ogg": "audio/ogg",
        }.get(ext, "audio/unknown")
    return None


def category(ext: str) -> str:
    ext = ext.lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in AUDIO_EXTENSIONS or ext in MEDIA_EXTENSIONS:
        return "media"
    if ext in TEXT_EXTENSIONS:
        return "text"
    return "other"
