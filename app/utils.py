import os

def human_size(n: int | float) -> str:
    try:
        n = float(n)
    except Exception:
        return "unknown"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024.0
        i += 1
    return f"{n:.2f} {units[i]}"

def is_video_ext(name: str) -> bool:
    ext = os.path.splitext(name.lower())[-1]
    return ext in {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v"}
