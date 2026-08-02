"""统一构造 yt-dlp 选项，缓解 YouTube 403（反爬/风控）。

支持通过环境变量增强下载稳定性：
- YTDLP_COOKIES_FROM_BROWSER：从浏览器读 Cookie，如 "chrome" / "edge" / "firefox"，
  可带 profile，如 "chrome:Default"。（最有效的 403 解法）
- YTDLP_COOKIES_FILE：cookies.txt 文件路径（Netscape 格式）。
- YTDLP_PLAYER_CLIENT：指定 player client，逗号分隔，如 "web_safari,android,tv"。
"""

import os

from . import config  # noqa: F401  导入即加载 .env

# 仅用于直接抓取 json3 字幕的 urllib 请求（不经 yt-dlp）；不要注入进 yt-dlp 选项。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _cookies_from_browser():
    raw = os.environ.get("YTDLP_COOKIES_FROM_BROWSER", "").strip()
    if not raw:
        return None
    browser, _, profile = raw.partition(":")
    browser = browser.strip()
    if not browser:
        return None
    # yt-dlp 期望元组：(BROWSER, PROFILE, KEYRING, CONTAINER)
    return (browser, profile.strip() or None, None, None)


def base_ydl_opts() -> dict:
    # 不手动覆盖 UA/请求头：新版 yt-dlp 按内部 client 自管，强制全局 UA 反而易触发 403。
    opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
    }

    player_client = os.environ.get("YTDLP_PLAYER_CLIENT", "").strip()
    if player_client:
        clients = [c.strip() for c in player_client.split(",") if c.strip()]
        opts["extractor_args"] = {"youtube": {"player_client": clients}}

    cookies_browser = _cookies_from_browser()
    if cookies_browser:
        opts["cookiesfrombrowser"] = cookies_browser

    cookiefile = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
    if cookiefile:
        opts["cookiefile"] = cookiefile

    return opts
