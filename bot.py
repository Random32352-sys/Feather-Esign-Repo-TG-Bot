"""
ESign Repository Bot - Standalone Telegram Bot for IPA Repository Management
Manages ESign/Feather app repository on Windows IIS with fast Telethon downloads.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

import aiofiles
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ParseMode
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename

# =============================================================================
# CONFIGURATION
# =============================================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
ESIGN_PORT = int(os.getenv("ESIGN_PORT", "80"))

# GitHub Configuration
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_OWNER = os.getenv("GITHUB_OWNER", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")

# Repository URL (public repository URL)
# Using GitHub Pages for hosting
REPO_URL = os.getenv("REPO_URL", "https://woomc.xyz/repo")

# Project paths
PROJECT_PATH = Path(__file__).parent
IPA_PATH = PROJECT_PATH / "IPA"
ESIGN_PATH = PROJECT_PATH / "repo" / "esign"
VERSIONS_PATH = IPA_PATH / "versions"
MAIN_IPA_PATH = IPA_PATH / "soundcloud.ipa"
SOURCE_JSON_PATH = ESIGN_PATH / "source.json"
VERSION_HISTORY_PATH = ESIGN_PATH / "version_history.json"
CHANGELOG_PATH = ESIGN_PATH / "changelog.txt"

# Session file for Telethon
SESSION_NAME = "esignbot_session"

# Max versions to keep
MAX_VERSIONS = 20

# Executor for non-blocking file operations
executor = ThreadPoolExecutor(max_workers=2)

# Global Telethon client (initialized in main)
telethon_client: TelegramClient = None

# =============================================================================
# LOGGING SETUP
# =============================================================================

# Suppress verbose logging from libraries
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)

# Configure our logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)s │ %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("esignbot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("ESignBot")

# =============================================================================
# FSM STATES
# =============================================================================


class UploadStates(StatesGroup):
    waiting_for_ipa = State()
    waiting_for_confirmation = State()


class ChangelogStates(StatesGroup):
    waiting_for_changelog = State()


class DescriptionStates(StatesGroup):
    waiting_for_description = State()


class DeleteVersionStates(StatesGroup):
    waiting_for_confirmation = State()


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================


def get_local_ip() -> str:
    """Get local network IP using socket method (not localhost)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def extract_version_from_filename(filename: str) -> str:
    """Extract version number from IPA filename using regex."""
    # Match patterns like "SoundCloud 8.42.0 NoAds.ipa" or "App_1.2.3.ipa"
    patterns = [
        r"(\d+\.\d+\.\d+)",  # Match x.y.z
        r"(\d+\.\d+)",  # Match x.y
    ]
    for pattern in patterns:
        match = re.search(pattern, filename)
        if match:
            return match.group(1)
    return "unknown"


def get_repository_url() -> str:
    """Generate repository URL from detected IP and port."""
    ip = get_local_ip()
    if ESIGN_PORT == 80:
        return f"http://{ip}/esign"
    return f"http://{ip}:{ESIGN_PORT}/esign"


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size."""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} TB"


async def load_changelog() -> str:
    """Load current changelog from file."""
    try:
        if CHANGELOG_PATH.exists():
            async with aiofiles.open(CHANGELOG_PATH, "r", encoding="utf-8") as f:
                return (await f.read()).strip()
    except Exception as e:
        logger.error(f"Error loading changelog: {e}")
    return "No changelog specified"


async def save_changelog(text: str) -> bool:
    """Save changelog atomically using tmp file pattern."""
    try:
        tmp_path = CHANGELOG_PATH.with_suffix(".tmp")
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(text)
        tmp_path.replace(CHANGELOG_PATH)
        return True
    except Exception as e:
        logger.error(f"Error saving changelog: {e}")
        return False


async def load_version_history() -> dict:
    """Load version history from JSON file."""
    try:
        if VERSION_HISTORY_PATH.exists():
            async with aiofiles.open(VERSION_HISTORY_PATH, "r", encoding="utf-8") as f:
                return json.loads(await f.read())
    except Exception as e:
        logger.error(f"Error loading version history: {e}")
    return {
        "app": "SoundCloud",
        "bundleIdentifier": "com.soundcloud.TouchApp",
        "current_version": "",
        "versions": [],
    }


async def save_version_history(history: dict) -> bool:
    """Save version history atomically using tmp file pattern."""
    try:
        tmp_path = VERSION_HISTORY_PATH.with_suffix(".tmp")
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(history, indent=2))
        tmp_path.replace(VERSION_HISTORY_PATH)
        return True
    except Exception as e:
        logger.error(f"Error saving version history: {e}")
        return False


async def load_source_json() -> dict:
    """Load source.json for ESign/Feather."""
    try:
        if SOURCE_JSON_PATH.exists():
            async with aiofiles.open(SOURCE_JSON_PATH, "r", encoding="utf-8") as f:
                return json.loads(await f.read())
    except Exception as e:
        logger.error(f"Error loading source.json: {e}")
    return {"apps": []}


async def save_source_json(source: dict) -> bool:
    """Save source.json atomically."""
    try:
        tmp_path = SOURCE_JSON_PATH.with_suffix(".tmp")
        async with aiofiles.open(tmp_path, "w", encoding="utf-8") as f:
            await f.write(json.dumps(source, indent=2))
        tmp_path.replace(SOURCE_JSON_PATH)
        return True
    except Exception as e:
        logger.error(f"Error saving source.json: {e}")
        return False


def get_placeholder_source() -> dict:
    """Get the placeholder source.json structure for when repo is empty."""
    now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "name": "woomc repo",
        "identifier": "xyz.woomc.repo",
        "iconURL": f"{REPO_URL}/esign/logo.png",
        "website": REPO_URL,
        "sourceURL": f"{REPO_URL}/esign/source.json",
        "apps": [
            {
                "name": "Coming Soon",
                "bundleIdentifier": "com.placeholder.app",
                "developerName": "woomc",
                "iconURL": f"{REPO_URL}/esign/logo.png",
                "localizedDescription": "More apps will be added here soon!",
                "subtitle": "Stay tuned",
                "tintColor": "808080",
                "versions": [
                    {
                        "version": "1.0",
                        "date": now_iso,
                        "size": 0,
                        "downloadURL": f"{REPO_URL}/esign/source.json"
                    }
                ],
                "appPermissions": {},
                "screenshotURLs": [],
                "version": "1.0",
                "versionDate": now_iso,
                "size": 0,
                "downloadURL": f"{REPO_URL}/esign/source.json"
            }
        ],
        "news": []
    }


def is_repo_empty(source: dict) -> bool:
    """Check if the repository has no real apps (only placeholder or empty)."""
    apps = source.get("apps", [])
    if not apps:
        return True
    # Check if only placeholder exists
    for app in apps:
        bundle_id = app.get("bundleIdentifier", "")
        if bundle_id != "com.placeholder.app":
            # Has a real app with versions
            if app.get("versions") and len(app.get("versions", [])) > 0:
                return False
    return True


# =============================================================================
# GITHUB API FUNCTIONS
# =============================================================================


async def upload_to_github_release(file_path: Path, version: str, changelog: str) -> Optional[str]:
    """Upload IPA to GitHub Releases and return the download URL."""
    # #region agent log
    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:247", "message": "upload_to_github_release entry", "data": {"version": version, "file_path": str(file_path), "file_exists": file_path.exists(), "has_token": bool(GITHUB_TOKEN), "has_owner": bool(GITHUB_OWNER), "has_repo": bool(GITHUB_REPO)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
    # #endregion
    if not GITHUB_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.error("GitHub configuration missing")
        # #region agent log
        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:252", "message": "GitHub config missing", "data": {"has_token": bool(GITHUB_TOKEN), "has_owner": bool(GITHUB_OWNER), "has_repo": bool(GITHUB_REPO)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
        # #endregion
        return None
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # Check if release exists for this version
            tag_name = f"v{version}"
            release_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{tag_name}"
            
            async with session.get(release_url, headers=headers) as resp:
                if resp.status == 200:
                    release_data = await resp.json()
                    release_id = release_data["id"]
                    logger.info(f"Found existing release: {tag_name}")
                    
                    # Delete existing asset if present
                    for asset in release_data.get("assets", []):
                        if asset["name"] == "soundcloud.ipa":
                            delete_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/assets/{asset['id']}"
                            async with session.delete(delete_url, headers=headers) as del_resp:
                                if del_resp.status == 204:
                                    logger.info("Deleted old IPA asset")
                else:
                    # Create new release
                    create_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
                    release_payload = {
                        "tag_name": tag_name,
                        "name": f"SoundCloud {version}",
                        "body": changelog,
                        "draft": False,
                        "prerelease": False,
                    }
                    async with session.post(create_url, headers=headers, json=release_payload) as create_resp:
                        if create_resp.status == 201:
                            release_data = await create_resp.json()
                            release_id = release_data["id"]
                            logger.info(f"Created new release: {tag_name}")
                        else:
                            error = await create_resp.text()
                            logger.error(f"Failed to create release: {error}")
                            return None
            
            # Upload the IPA as an asset
            upload_url = f"https://uploads.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/{release_id}/assets?name=soundcloud.ipa"
            upload_headers = {
                **headers,
                "Content-Type": "application/octet-stream",
            }
            
            # Read file and upload
            async with aiofiles.open(file_path, "rb") as f:
                file_data = await f.read()
            
            async with session.post(upload_url, headers=upload_headers, data=file_data) as upload_resp:
                # #region agent log
                log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:308", "message": "GitHub asset upload response", "data": {"status": upload_resp.status, "file_size": len(file_data)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                # #endregion
                if upload_resp.status == 201:
                    asset_data = await upload_resp.json()
                    download_url = asset_data["browser_download_url"]
                    logger.info(f"Uploaded IPA to GitHub: {download_url}")
                    # #region agent log
                    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:312", "message": "GitHub upload success", "data": {"download_url": download_url}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                    # #endregion
                    return download_url
                else:
                    error = await upload_resp.text()
                    logger.error(f"Failed to upload asset: {error}")
                    # #region agent log
                    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:316", "message": "GitHub upload failed", "data": {"status": upload_resp.status, "error": error[:200]}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                    # #endregion
                    return None
                    
    except Exception as e:
        logger.error(f"GitHub upload error: {e}")
        # #region agent log
        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:320", "message": "GitHub upload exception", "data": {"error": str(e)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
        # #endregion
        return None


async def delete_github_release(version: str) -> bool:
    """Delete a GitHub release by version tag."""
    if not GITHUB_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.error("GitHub configuration missing")
        return False
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            tag_name = f"v{version}"
            release_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{tag_name}"
            
            # Get release info
            async with session.get(release_url, headers=headers) as resp:
                if resp.status == 200:
                    release_data = await resp.json()
                    release_id = release_data["id"]
                    
                    # Delete the release
                    delete_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/{release_id}"
                    async with session.delete(delete_url, headers=headers) as del_resp:
                        if del_resp.status == 204:
                            logger.info(f"Deleted GitHub release: {tag_name}")
                            return True
                        else:
                            error = await del_resp.text()
                            logger.error(f"Failed to delete release: {error}")
                            return False
                else:
                    logger.warning(f"Release {tag_name} not found on GitHub")
                    return False
                    
    except Exception as e:
        logger.error(f"GitHub release deletion error: {e}")
        return False


async def push_file_to_github(file_path: Path, repo_path: str, commit_message: str) -> bool:
    """Push a file to GitHub repository."""
    if not GITHUB_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.error("GitHub configuration missing")
        return False
    
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            # Get current file SHA (if exists)
            file_url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/{repo_path}"
            sha = None
            
            async with session.get(file_url, headers=headers) as resp:
                if resp.status == 200:
                    file_data = await resp.json()
                    sha = file_data.get("sha")
            
            # Read file content
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                content = await f.read()
            
            # #region agent log
            log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "D", "location": "bot.py:407", "message": "Content being pushed to GitHub", "data": {"content_preview": content[:300], "content_length": len(content), "sha_found": sha is not None, "repo_path": repo_path}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
            # #endregion
            
            import base64
            encoded_content = base64.b64encode(content.encode()).decode()
            
            # Update/create file
            payload = {
                "message": commit_message,
                "content": encoded_content,
            }
            if sha:
                payload["sha"] = sha
            
            async with session.put(file_url, headers=headers, json=payload) as resp:
                resp_body = await resp.text()
                # #region agent log
                log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "D", "location": "bot.py:420", "message": "GitHub file push response", "data": {"status": resp.status, "repo_path": repo_path, "response_preview": resp_body[:300] if resp_body else "empty"}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                # #endregion
                if resp.status in [200, 201]:
                    logger.info(f"Pushed {repo_path} to GitHub")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Failed to push file: {error}")
                    # #region agent log
                    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "bot.py:367", "message": "GitHub push failed", "data": {"status": resp.status, "error": error[:200]}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                    # #endregion
                    return False
                    
    except Exception as e:
        logger.error(f"GitHub push error: {e}")
        # #region agent log
        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "bot.py:371", "message": "GitHub push exception", "data": {"error": str(e)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
        # #endregion
        return False


def get_github_download_url(version: str) -> str:
    """Get the GitHub Releases download URL for an IPA."""
    return f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/download/v{version}/soundcloud.ipa"


def copy_backup_sync(src: Path, dst: Path) -> bool:
    """Synchronous file copy for use with executor."""
    try:
        shutil.copy2(src, dst)
        return True
    except Exception as e:
        logger.error(f"Error copying backup: {e}")
        return False


async def copy_backup_async(src: Path, dst: Path) -> bool:
    """Non-blocking file copy using executor."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(executor, copy_backup_sync, src, dst)


async def cleanup_old_versions(history: dict) -> None:
    """Remove old versions when exceeding MAX_VERSIONS."""
    versions = history.get("versions", [])
    if len(versions) > MAX_VERSIONS:
        # Sort by date, oldest first
        versions_sorted = sorted(versions, key=lambda x: x.get("date", ""))
        to_remove = versions_sorted[: len(versions) - MAX_VERSIONS]
        
        for v in to_remove:
            filename = v.get("filename", "")
            if filename:
                file_path = VERSIONS_PATH / filename
                if file_path.exists():
                    try:
                        file_path.unlink()
                        logger.info(f"Removed old version: {filename}")
                    except Exception as e:
                        logger.error(f"Error removing old version {filename}: {e}")
        
        # Update history to keep only recent versions
        history["versions"] = versions_sorted[len(versions) - MAX_VERSIONS :]


async def ensure_esign_setup() -> bool:
    """Create all required directories and files on first run."""
    try:
        # Create main directory (including parents)
        ESIGN_PATH.mkdir(parents=True, exist_ok=True)
        
        # Create versions subfolder
        VERSIONS_PATH.mkdir(parents=True, exist_ok=True)
        
        # Test write permission
        test_file = ESIGN_PATH / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            logger.error(f"No write permission to {ESIGN_PATH}")
            return False
        
        # Create changelog.txt if missing
        if not CHANGELOG_PATH.exists():
            await save_changelog("Latest version")
        
        # Create version_history.json if missing
        if not VERSION_HISTORY_PATH.exists():
            await save_version_history({
                "app": "SoundCloud",
                "bundleIdentifier": "com.soundcloud.TouchApp",
                "current_version": "",
                "versions": [],
            })
        
        # Create source.json if missing
        if not SOURCE_JSON_PATH.exists():
            await save_source_json({"apps": []})
        
        logger.info(f"ESign setup complete at {ESIGN_PATH}")
        return True
        
    except Exception as e:
        logger.error(f"Error during setup: {e}")
        return False


# =============================================================================
# PROGRESS TRACKER
# =============================================================================


class ProgressTracker:
    """Track download progress with speed, ETA, and percentage."""

    def __init__(self, bot: Bot, chat_id: int, message_id: int, total_size: int):
        self.bot = bot
        self.chat_id = chat_id
        self.message_id = message_id
        self.total_size = total_size
        self.downloaded = 0
        self.start_time = datetime.now()
        self.last_update = datetime.now()
        self.update_interval = 1.5  # seconds

    def make_progress_bar(self, percentage: float, length: int = 20) -> str:
        """Create a text-based progress bar."""
        filled = int(length * percentage / 100)
        empty = length - filled
        return "█" * filled + "░" * empty

    async def update(self, current: int, total: int) -> None:
        """Update progress display (rate-limited)."""
        self.downloaded = current
        now = datetime.now()
        elapsed = (now - self.last_update).total_seconds()

        if elapsed < self.update_interval:
            return

        self.last_update = now
        total_elapsed = (now - self.start_time).total_seconds()

        # Calculate metrics
        percentage = (current / total * 100) if total > 0 else 0
        speed = current / total_elapsed if total_elapsed > 0 else 0
        remaining = total - current
        eta = remaining / speed if speed > 0 else 0

        # Format display
        progress_bar = self.make_progress_bar(percentage)
        speed_str = format_size(speed) + "/s"
        eta_str = f"{int(eta // 60)}m {int(eta % 60)}s" if eta > 0 else "calculating..."

        text = (
            f"📥 **Downloading IPA...**\n\n"
            f"`{progress_bar}` {percentage:.1f}%\n\n"
            f"📊 **Progress:** {format_size(current)} / {format_size(total)}\n"
            f"⚡ **Speed:** {speed_str}\n"
            f"⏱️ **ETA:** {eta_str}"
        )

        try:
            await self.bot.edit_message_text(
                text=text,
                chat_id=self.chat_id,
                message_id=self.message_id,
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception:
            pass  # Ignore rate limit errors


# =============================================================================
# BOT SETUP
# =============================================================================

router = Router()

# Store pending uploads {user_id: {file_id, filename, version, size, message_id}}
pending_uploads: dict = {}


def is_owner(user_id: int) -> bool:
    """Check if user is the bot owner."""
    return user_id == OWNER_ID


def build_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Build confirmation keyboard for IPA upload."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Confirm Upload", callback_data="confirm_upload"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_upload"),
            ],
            [
                InlineKeyboardButton(text="📝 Edit Changelog", callback_data="edit_changelog"),
                InlineKeyboardButton(text="📋 Edit Description", callback_data="edit_description"),
            ],
        ]
    )


def build_version_delete_keyboard(versions: list) -> InlineKeyboardMarkup:
    """Build keyboard with version buttons for deletion."""
    buttons = []
    # Group buttons in rows of 2
    for i in range(0, len(versions), 2):
        row = []
        for j in range(2):
            if i + j < len(versions):
                version = versions[i + j]
                version_str = version.get("version", "unknown")
                date_str = version.get("date", "")[:10] if version.get("date") else ""
                # Check if this is an untracked file (has "path" key)
                is_untracked = "path" in version
                # Check if this is a GitHub release (has "source" key)
                is_github = version.get("source") == "github"
                
                if is_untracked:
                    prefix = "⚠️"
                    display_name = version.get("filename", version_str)
                    button_text = f"{prefix} {display_name}"
                elif is_github:
                    prefix = "🌐"
                    # Show app name if available, otherwise version
                    app_name = version.get("app_name", "")
                    if app_name:
                        button_text = f"{prefix} {app_name} v{version_str}"
                    else:
                        button_text = f"{prefix} {version_str}"
                else:
                    prefix = "🗑️"
                    button_text = f"{prefix} {version_str}"
                    
                if date_str:
                    button_text += f" ({date_str})"
                    
                # Include type marker in callback data
                if is_untracked:
                    callback_data = f"delete_version:{version_str}:untracked:{version.get('filename', '')}"
                elif is_github:
                    callback_data = f"delete_version:{version_str}:github"
                else:
                    callback_data = f"delete_version:{version_str}:tracked"
                row.append(InlineKeyboardButton(
                    text=button_text,
                    callback_data=callback_data
                ))
        buttons.append(row)
    
    # Add cancel button
    buttons.append([
        InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_delete")
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# =============================================================================
# COMMAND HANDLERS
# =============================================================================


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Show help and features."""
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Access denied. This bot is private.")
        return

    source_url = f"{REPO_URL}/esign/source.json"
    help_text = (
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "🚀 **woomc Repo Bot**\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        
        "📤 **Upload & Manage**\n"
        "├ /send — Upload new IPA\n"
        "├ /deleteversion — Remove a version\n"
        "└ /cleanreleases — Clean orphaned releases\n\n"
        
        "✏️ **Edit Content**\n"
        "├ /setchangelog — Set changelog\n"
        "└ /setdescription — Set app description\n\n"
        
        "📊 **Status & Sync**\n"
        "├ /repoinfo — Repository status\n"
        "├ /syncgithub — Force push to GitHub\n"
        "└ /stop — Cancel current operation\n\n"
        
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🌐 **Repo:** `{REPO_URL}`\n"
        f"📱 **Source:** `{source_url}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext) -> None:
    """Start the IPA upload wizard."""
    if not is_owner(message.from_user.id):
        return

    # Clear any existing state
    await state.clear()
    pending_uploads.pop(message.from_user.id, None)
    
    await state.set_state(UploadStates.waiting_for_ipa)
    
    changelog = await load_changelog()
    
    await message.answer(
        "📤 **Upload IPA**\n\n"
        "Send or forward an IPA file to upload.\n\n"
        f"📝 **Current changelog:**\n_{changelog}_\n\n"
        "💡 **Tip:** Forward from Saved Messages for fast upload!\n\n"
        "❌ Cancel: /stop",
        parse_mode=ParseMode.MARKDOWN,
    )


@router.message(Command("stop"))
async def cmd_stop(message: Message, state: FSMContext) -> None:
    """Cancel any ongoing operation."""
    if not is_owner(message.from_user.id):
        return

    current_state = await state.get_state()
    
    # Clear state and pending uploads
    await state.clear()
    pending_uploads.pop(message.from_user.id, None)
    
    if current_state:
        await message.answer(
            "⏹️ **Operation cancelled.**",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer(
            "ℹ️ Nothing to cancel.",
            parse_mode=ParseMode.MARKDOWN,
        )


@router.message(Command("repoinfo"))
async def cmd_repoinfo(message: Message) -> None:
    """Display comprehensive repository status with sync check."""
    if not is_owner(message.from_user.id):
        return

    await message.answer("🔍 **Checking repository status...**", parse_mode=ParseMode.MARKDOWN)

    # Load all data sources
    history = await load_version_history()
    source = await load_source_json()
    
    # Get versions from each source
    history_version = history.get("current_version", "")
    history_versions = history.get("versions", [])
    
    # Get version from source.json (what ESign/Feather sees)
    source_apps = source.get("apps", [])
    source_version = ""
    source_download_url = ""
    if source_apps:
        # Find the SoundCloud app (or first app with versions)
        for app in source_apps:
            if app.get("versions"):
                source_version = app["versions"][0].get("version", "")
                source_download_url = app["versions"][0].get("downloadURL", "")
                break
    
    # Check local IPA
    ipa_exists = MAIN_IPA_PATH.exists()
    ipa_size = format_size(MAIN_IPA_PATH.stat().st_size) if ipa_exists else "N/A"
    
    # Check GitHub Release exists
    github_release_exists = False
    github_version = ""
    if GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO and history_version:
        try:
            headers = {
                "Authorization": f"token {GITHUB_TOKEN}",
                "Accept": "application/vnd.github.v3+json",
            }
            async with aiohttp.ClientSession() as session:
                tag_name = f"v{history_version}"
                url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/tags/{tag_name}"
                async with session.get(url, headers=headers) as resp:
                    if resp.status == 200:
                        github_release_exists = True
                        github_version = history_version
        except Exception:
            pass
    
    # Determine sync status
    all_synced = True
    sync_issues = []
    
    if history_version and source_version and history_version != source_version:
        all_synced = False
        sync_issues.append(f"• version\\_history: `{history_version}` ≠ source.json: `{source_version}`")
    
    if history_version and not github_release_exists:
        all_synced = False
        sync_issues.append(f"• GitHub Release `v{history_version}` not found")
    
    if history_version and not ipa_exists:
        sync_issues.append("• Local IPA file missing")
    
    # Build status
    sync_status = "✅ **All synced**" if all_synced and history_version else "⚠️ **Out of sync**" if sync_issues else "ℹ️ **No version uploaded yet**"
    
    # Build output
    text = "📦 **Repository Status**\n\n"
    
    # Version info section
    text += "**📱 Versions:**\n"
    text += f"• History: `{history_version or 'None'}`\n"
    text += f"• source.json: `{source_version or 'None'}`\n"
    text += f"• GitHub Release: {'✅' if github_release_exists else '❌'} `{github_version or 'None'}`\n\n"
    
    # Local files section
    text += "**💾 Local Files:**\n"
    text += f"• Main IPA: {'✅' if ipa_exists else '❌'} ({ipa_size})\n"
    text += f"• Backups: {len(history_versions)} versions\n\n"
    
    # Sync status section
    text += f"**🔄 Sync Status:** {sync_status}\n"
    if sync_issues:
        text += "\n".join(sync_issues) + "\n"
    text += "\n"
    
    # URLs section
    text += f"**🌐 Repository:** `{REPO_URL}`\n"
    if history_version:
        text += f"**📥 Download:** `{get_github_download_url(history_version)}`"
    
    await message.answer(text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("setchangelog"))
async def cmd_setchangelog(message: Message) -> None:
    """Set changelog for next upload."""
    if not is_owner(message.from_user.id):
        return

    # Extract text after command
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📝 **Usage:** `/setchangelog Your changelog text here`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    changelog_text = parts[1].strip()
    if await save_changelog(changelog_text):
        await message.answer(
            f"✅ Changelog updated:\n\n_{changelog_text}_",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer("❌ Failed to save changelog. Check permissions.")


@router.message(Command("setdescription"))
async def cmd_setdescription(message: Message) -> None:
    """Set app description in source.json."""
    if not is_owner(message.from_user.id):
        return

    # Extract text after command
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(
            "📝 **Usage:** /setdescription Your app description here",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    description_text = parts[1].strip()
    
    # Load source.json and update description
    source = await load_source_json()
    apps = source.get("apps", [])
    
    if not apps:
        await message.answer("❌ No apps in source.json yet. Upload an IPA first.")
        return
    
    # Update the first app's description
    apps[0]["localizedDescription"] = description_text
    
    # Save and push to GitHub
    if await save_source_json(source):
        # Push to GitHub
        push_success = await push_file_to_github(
            SOURCE_JSON_PATH,
            "repo/esign/source.json",
            f"Update app description"
        )
        
        if push_success:
            await message.answer(
                f"✅ **Description updated and pushed to GitHub!**\n\n_{description_text}_",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.answer(
                f"✅ Description saved locally, but failed to push to GitHub.\n\n_{description_text}_",
                parse_mode=ParseMode.MARKDOWN,
            )
    else:
        await message.answer("❌ Failed to save description. Check permissions.")


@router.message(Command("syncgithub"))
async def cmd_syncgithub(message: Message) -> None:
    """Force push source.json to GitHub."""
    if not is_owner(message.from_user.id):
        return

    await message.answer("🔄 **Pushing source.json to GitHub...**", parse_mode=ParseMode.MARKDOWN)
    
    try:
        if not GITHUB_TOKEN:
            await message.answer("❌ GitHub token not configured", parse_mode=ParseMode.MARKDOWN)
            return
        
        success = await push_file_to_github(
            SOURCE_JSON_PATH,
            "repo/esign/source.json",
            "Force sync source.json"
        )
        
        if success:
            await message.answer(
                "✅ **Successfully pushed source.json to GitHub!**\n\n"
                "GitHub Pages should update in a few seconds.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.answer(
                "❌ **Failed to push to GitHub**\n\nCheck logs for details.",
                parse_mode=ParseMode.MARKDOWN,
            )
    except Exception as e:
        await message.answer(f"❌ **Error:** `{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.message(Command("cleanreleases"))
async def cmd_cleanreleases(message: Message) -> None:
    """Find and delete orphaned GitHub Releases not in source.json."""
    if not is_owner(message.from_user.id):
        return

    if not GITHUB_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        await message.answer("❌ GitHub not configured", parse_mode=ParseMode.MARKDOWN)
        return

    await message.answer("🔍 **Scanning GitHub Releases...**", parse_mode=ParseMode.MARKDOWN)

    try:
        # Get versions from source.json
        source = await load_source_json()
        source_versions = set()
        if source.get("apps"):
            for app in source["apps"]:
                for version in app.get("versions", []):
                    source_versions.add(version.get("version", ""))

        # Get GitHub releases
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json",
        }
        
        orphaned_releases = []
        
        async with aiohttp.ClientSession() as session:
            url = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases"
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    releases = await resp.json()
                    for release in releases:
                        tag = release.get("tag_name", "")
                        # Remove 'v' prefix to match version
                        version = tag.lstrip("v")
                        if version and version not in source_versions:
                            orphaned_releases.append({
                                "tag": tag,
                                "version": version,
                                "name": release.get("name", ""),
                                "id": release.get("id"),
                            })

        if not orphaned_releases:
            await message.answer(
                "✅ **No orphaned releases found!**\n\n"
                "All GitHub Releases are properly tracked in source.json.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        # Build keyboard for orphaned releases
        buttons = []
        for release in orphaned_releases:
            buttons.append([
                InlineKeyboardButton(
                    text=f"🗑️ Delete {release['name']} ({release['tag']})",
                    callback_data=f"clean_release:{release['tag']}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_clean")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        await message.answer(
            f"⚠️ **Found {len(orphaned_releases)} Orphaned Release(s)**\n\n"
            "These releases exist on GitHub but are NOT in source.json:\n\n"
            + "\n".join([f"• `{r['tag']}` - {r['name']}" for r in orphaned_releases]) +
            "\n\nSelect a release to delete:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard,
        )

    except Exception as e:
        await message.answer(f"❌ **Error:** `{str(e)}`", parse_mode=ParseMode.MARKDOWN)


@router.callback_query(F.data.startswith("clean_release:"))
async def callback_clean_release(callback: CallbackQuery) -> None:
    """Delete an orphaned GitHub Release."""
    if not is_owner(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    tag = callback.data.split(":")[1]
    version = tag.lstrip("v")
    
    await callback.answer("Deleting release...")
    
    success = await delete_github_release(version)
    
    if success:
        await callback.message.edit_text(
            f"✅ **Deleted orphaned release!**\n\n"
            f"🗑️ Release `{tag}` has been removed from GitHub.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await callback.message.edit_text(
            f"❌ **Failed to delete release**\n\n"
            f"Release `{tag}` could not be deleted. Check logs.",
            parse_mode=ParseMode.MARKDOWN,
        )


@router.callback_query(F.data == "cancel_clean")
async def callback_cancel_clean(callback: CallbackQuery) -> None:
    """Cancel clean releases operation."""
    if not is_owner(callback.from_user.id):
        return
    await callback.message.edit_text("❌ Cleanup cancelled.")
    await callback.answer()


@router.message(Command("deleteversion"))
async def cmd_deleteversion(message: Message) -> None:
    """Show list of versions to delete."""
    if not is_owner(message.from_user.id):
        return

    history = await load_version_history()
    versions = history.get("versions", [])
    
    # Also check for untracked IPA files
    untracked_files = []
    if VERSIONS_PATH.exists():
        tracked_filenames = {v.get("filename", "") for v in versions}
        for file_path in VERSIONS_PATH.glob("*.ipa"):
            if file_path.name not in tracked_filenames:
                # Try to extract version from filename
                version_match = re.search(r"soundcloud_([\d.]+)\.ipa", file_path.name)
                if version_match:
                    untracked_version = version_match.group(1)
                else:
                    untracked_version = "unknown"
                untracked_files.append({
                    "filename": file_path.name,
                    "version": untracked_version,
                    "size": file_path.stat().st_size,
                    "date": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d"),
                })
    
    # Check main IPA file in esign directory if it exists
    esign_ipa = ESIGN_PATH / "soundcloud.ipa"
    if esign_ipa.exists():
        # Check if it's tracked
        is_tracked = any(v.get("filename") == "soundcloud.ipa" or MAIN_IPA_PATH == esign_ipa for v in versions)
        if not is_tracked:
            # Use a unique identifier for this untracked file
            untracked_files.append({
                "filename": "soundcloud.ipa",
                "version": "esign/soundcloud.ipa",  # Unique identifier
                "size": esign_ipa.stat().st_size,
                "date": datetime.fromtimestamp(esign_ipa.stat().st_mtime).strftime("%Y-%m-%d"),
                "path": "esign",  # Mark as untracked
            })
    
    # Also check source.json for versions that aren't tracked locally (GitHub releases)
    source_versions = []
    source = await load_source_json()
    if source.get("apps"):
        tracked_versions = {v.get("version", "") for v in versions}
        for app in source["apps"]:
            app_name = app.get("name", "Unknown App")
            if app.get("versions"):
                for sv in app["versions"]:
                    sv_version = sv.get("version", "")
                    if sv_version and sv_version not in tracked_versions:
                        # This version is in source.json but not in version_history
                        source_versions.append({
                            "version": sv_version,
                            "app_name": app_name,  # Include app name for display
                            "date": sv.get("date", "")[:10] if sv.get("date") else "",
                            "size": sv.get("size", 0),
                            "downloadURL": sv.get("downloadURL", ""),
                            "source": "github",  # Mark as GitHub release
                        })
    
    if not versions and not untracked_files and not source_versions:
        await message.answer(
            "📦 **No versions found**\n\nThere are no versions to delete.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return
    
    # Sort versions by date (newest first)
    versions_sorted = sorted(versions, key=lambda x: x.get("date", ""), reverse=True)
    
    # Build message text
    current_version = history.get("current_version", "")
    text = "🗑️ **Delete Version**\n\n"
    text += f"📚 **Tracked versions:** {len(versions)}\n"
    if untracked_files:
        text += f"⚠️ **Untracked files:** {len(untracked_files)}\n"
    if source_versions:
        text += f"🌐 **GitHub releases:** {len(source_versions)}\n"
    text += f"📱 **Current version:** `{current_version or 'None'}`\n\n"
    text += "Select a version to delete:"
    
    # Combine tracked, untracked, and source versions for display
    all_items = versions_sorted + untracked_files + source_versions
    
    await message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_version_delete_keyboard(all_items),
    )


# =============================================================================
# DOCUMENT HANDLER (only active after /send)
# =============================================================================


@router.message(F.document, StateFilter(UploadStates.waiting_for_ipa))
async def handle_document(message: Message, state: FSMContext) -> None:
    """Handle forwarded IPA files (only after /send)."""
    if not is_owner(message.from_user.id):
        return

    doc = message.document
    filename = doc.file_name or ""

    # Check if it's an IPA file
    if not filename.lower().endswith(".ipa"):
        await message.answer(
            "❌ **Not an IPA file**\n\nPlease send a `.ipa` file.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    logger.info(f"IPA detected: {filename}")

    # Extract version
    version = extract_version_from_filename(filename)
    size = doc.file_size
    changelog = await load_changelog()

    # Try to find matching file in Saved Messages (for fast Telethon download)
    saved_msg_id = None
    try:
        if telethon_client is None:
            logger.warning("Telethon client not initialized!")
        elif not telethon_client.is_connected():
            logger.warning("Telethon client not connected!")
        else:
            logger.info(f"Searching Saved Messages for: {filename}")
            saved_messages = await telethon_client.get_messages('me', limit=50)
            logger.info(f"Found {len(saved_messages)} messages in Saved Messages")
            
            for msg in saved_messages:
                if msg.media and hasattr(msg.media, 'document') and msg.media.document:
                    msg_doc = msg.media.document
                    msg_filename = None
                    for attr in msg_doc.attributes:
                        if hasattr(attr, 'file_name'):
                            msg_filename = attr.file_name
                            break
                    
                    if msg_filename:
                        logger.debug(f"Checking file: {msg_filename}")
                    
                    # Match by filename
                    if msg_filename and msg_filename == filename:
                        saved_msg_id = msg.id
                        logger.info(f"✅ Found IPA in Saved Messages, msg_id={saved_msg_id}")
                        break
            
            if not saved_msg_id:
                logger.info(f"❌ File '{filename}' not found in last 50 Saved Messages")
    except Exception as e:
        logger.warning(f"Could not search Saved Messages: {e}")

    # Store pending upload info
    pending_uploads[message.from_user.id] = {
        "file_id": doc.file_id,
        "filename": filename,
        "version": version,
        "size": size,
        "message_id": message.message_id,
        "chat_id": message.chat.id,
        "saved_msg_id": saved_msg_id,  # Will be None if not found in Saved Messages
    }

    # Transition to confirmation state
    await state.set_state(UploadStates.waiting_for_confirmation)

    # Show confirmation
    source_info = "⚡ **Fast download** (from Saved Messages)" if saved_msg_id else "📥 Will download via bot API"
    
    # Get current description
    source = await load_source_json()
    description = ""
    if source.get("apps") and len(source["apps"]) > 0:
        description = source["apps"][0].get("localizedDescription", "")
    if not description:
        description = "Not set"
    
    text = (
        "📦 **Detected IPA File**\n\n"
        f"📄 **Filename:** `{filename}`\n"
        f"🏷️ **Version:** `{version}`\n"
        f"💾 **Size:** {format_size(size)}\n"
        f"{source_info}\n\n"
        f"📋 **Description:**\n_{description}_\n\n"
        f"📝 **Changelog:**\n_{changelog}_\n\n"
        "Press **Confirm** to upload or edit fields below."
    )

    await message.answer(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_confirmation_keyboard(),
    )


# =============================================================================
# CALLBACK HANDLERS
# =============================================================================


@router.callback_query(F.data == "cancel_upload")
async def callback_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel pending upload."""
    if not is_owner(callback.from_user.id):
        return

    if callback.from_user.id in pending_uploads:
        del pending_uploads[callback.from_user.id]

    await state.clear()
    await callback.message.edit_text("❌ Upload cancelled.")
    await callback.answer()


@router.callback_query(F.data == "edit_changelog")
async def callback_edit_changelog(callback: CallbackQuery, state: FSMContext) -> None:
    """Start changelog editing."""
    if not is_owner(callback.from_user.id):
        return

    await state.set_state(ChangelogStates.waiting_for_changelog)
    await callback.message.answer(
        "📝 **Edit Changelog**\n\nSend me the new changelog text:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.message(StateFilter(ChangelogStates.waiting_for_changelog))
async def handle_changelog_input(message: Message, state: FSMContext) -> None:
    """Handle changelog text input."""
    if not is_owner(message.from_user.id):
        return

    new_changelog = message.text.strip()
    if await save_changelog(new_changelog):
        await message.answer(
            f"✅ Changelog updated:\n\n_{new_changelog}_\n\n"
            "Now click **Confirm Upload** to proceed.",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await message.answer("❌ Failed to save changelog.")

    await state.clear()


@router.callback_query(F.data == "edit_description")
async def callback_edit_description(callback: CallbackQuery, state: FSMContext) -> None:
    """Start description editing during upload."""
    if not is_owner(callback.from_user.id):
        return

    # Get current description
    source = await load_source_json()
    current_desc = ""
    if source.get("apps") and len(source["apps"]) > 0:
        current_desc = source["apps"][0].get("localizedDescription", "")

    await state.set_state(DescriptionStates.waiting_for_description)
    await callback.message.answer(
        f"📋 **Edit App Description**\n\n"
        f"**Current:** _{current_desc or 'Not set'}_\n\n"
        "Send me the new app description:",
        parse_mode=ParseMode.MARKDOWN,
    )
    await callback.answer()


@router.message(StateFilter(DescriptionStates.waiting_for_description))
async def handle_description_input(message: Message, state: FSMContext) -> None:
    """Handle description text input during upload."""
    if not is_owner(message.from_user.id):
        return

    new_description = message.text.strip()
    
    # Update source.json with new description
    source = await load_source_json()
    if source.get("apps") and len(source["apps"]) > 0:
        source["apps"][0]["localizedDescription"] = new_description
        if await save_source_json(source):
            await message.answer(
                f"✅ Description updated:\n\n_{new_description}_\n\n"
                "Now click **Confirm Upload** to proceed.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await message.answer("❌ Failed to save description.")
    else:
        await message.answer("❌ No app found in source.json.")

    await state.clear()


@router.callback_query(F.data == "cancel_delete")
async def callback_cancel_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """Cancel version deletion."""
    if not is_owner(callback.from_user.id):
        return

    await state.clear()
    await callback.message.edit_text("❌ Deletion cancelled.")
    await callback.answer()


@router.callback_query(F.data.startswith("delete_version:"))
async def callback_delete_version(callback: CallbackQuery, state: FSMContext) -> None:
    """Show confirmation dialog for version deletion."""
    if not is_owner(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    # Extract version, type, and optional filename from callback data
    parts = callback.data.split(":")
    version_to_delete = parts[1]
    delete_type = parts[2] if len(parts) > 2 else "tracked"
    filename_to_delete = parts[3] if len(parts) > 3 and delete_type == "untracked" else None
    
    # Load version info to show in confirmation
    history = await load_version_history()
    versions = history.get("versions", [])
    version_info = None
    is_untracked = False
    is_github = False
    
    if delete_type == "untracked" and filename_to_delete:
        # This is an untracked file
        file_path = VERSIONS_PATH / filename_to_delete if filename_to_delete != "soundcloud.ipa" else ESIGN_PATH / "soundcloud.ipa"
        if file_path.exists():
            version_info = {
                "version": version_to_delete,
                "filename": filename_to_delete,
                "size": file_path.stat().st_size,
                "date": datetime.fromtimestamp(file_path.stat().st_mtime).strftime("%Y-%m-%d"),
            }
            is_untracked = True
    elif delete_type == "github":
        # This is a GitHub release (in source.json but not tracked locally)
        source = await load_source_json()
        if source.get("apps"):
            for app in source["apps"]:
                if app.get("versions"):
                    for sv in app["versions"]:
                        if sv.get("version") == version_to_delete:
                            version_info = {
                                "version": version_to_delete,
                                "size": sv.get("size", 0),
                                "date": sv.get("date", ""),
                                "downloadURL": sv.get("downloadURL", ""),
                            }
                            is_github = True
                            break
    else:
        # Look for tracked version
        for v in versions:
            if v.get("version") == version_to_delete:
                version_info = v
                break
    
    if not version_info:
        await callback.answer("Version not found", show_alert=True)
        return
    
    # Store version and filename to delete in state
    await state.update_data(
        version_to_delete=version_to_delete, 
        filename_to_delete=filename_to_delete, 
        is_untracked=is_untracked,
        is_github=is_github
    )
    await state.set_state(DeleteVersionStates.waiting_for_confirmation)
    
    # Show confirmation message
    date_str = version_info.get("date", "")[:10] if version_info.get("date") else "Unknown date"
    size_str = format_size(version_info.get("size", 0))
    is_current = history.get("current_version") == version_to_delete
    
    if is_untracked:
        confirmation_text = (
            f"⚠️ **Confirm Deletion (Untracked File)**\n\n"
            f"🗑️ **Version:** `{version_to_delete}`\n"
            f"📄 **Filename:** `{filename_to_delete}`\n"
            f"📅 **Date:** {date_str}\n"
            f"💾 **Size:** {size_str}\n\n"
            f"⚠️ **This file is not tracked in version history!**\n\n"
        )
    elif is_github:
        confirmation_text = (
            f"🌐 **Confirm Deletion (GitHub Release)**\n\n"
            f"🗑️ **Version:** `{version_to_delete}`\n"
            f"📅 **Date:** {date_str}\n"
            f"💾 **Size:** {size_str}\n\n"
            f"⚠️ **This will delete the GitHub release!**\n\n"
        )
    else:
        confirmation_text = (
            f"⚠️ **Confirm Deletion**\n\n"
            f"🗑️ **Version:** `{version_to_delete}`\n"
            f"📅 **Date:** {date_str}\n"
            f"💾 **Size:** {size_str}\n"
        )
        if is_current:
            confirmation_text += "⚠️ **This is the current version!**\n\n"
    
    confirmation_text += "Are you sure you want to delete this version?\n\nThis action cannot be undone."
    
    # Create confirmation keyboard
    confirmation_keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, Delete", callback_data="confirm_delete_version"),
                InlineKeyboardButton(text="❌ Cancel", callback_data="cancel_delete"),
            ],
        ]
    )
    
    await callback.message.edit_text(
        confirmation_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=confirmation_keyboard,
    )
    await callback.answer()


@router.callback_query(F.data == "confirm_delete_version")
async def callback_confirm_delete_version(callback: CallbackQuery, state: FSMContext) -> None:
    """Actually delete the version after confirmation."""
    if not is_owner(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    # Get version to delete from state
    data = await state.get_data()
    version_to_delete = data.get("version_to_delete")
    filename_to_delete = data.get("filename_to_delete")
    is_untracked = data.get("is_untracked", False)
    is_github = data.get("is_github", False)
    
    if not version_to_delete:
        await callback.answer("No version selected", show_alert=True)
        await state.clear()
        return
    
    await callback.answer("Deleting version...")
    
    try:
        # Handle GitHub releases (in source.json but not tracked locally)
        if is_github:
            # Delete GitHub release
            release_deleted = await delete_github_release(version_to_delete)
            
            # Update source.json to remove the version
            source = await load_source_json()
            updated_source = False
            if source.get("apps"):
                for app in source["apps"]:
                    if app.get("versions"):
                        original_count = len(app["versions"])
                        app["versions"] = [v for v in app["versions"] if v.get("version") != version_to_delete]
                        if len(app["versions"]) < original_count:
                            updated_source = True
                            # Update app-level fields
                            if app.get("versions") and len(app["versions"]) > 0:
                                versions_sorted = sorted(app["versions"], key=lambda x: x.get("date", ""), reverse=True)
                                latest = versions_sorted[0]
                                app["version"] = latest.get("version", "")
                                app["versionDate"] = latest.get("date", "")
                                app["size"] = latest.get("size", 0)
                                app["downloadURL"] = latest.get("downloadURL", "")
                            else:
                                app["version"] = ""
                                app["versionDate"] = ""
                                app["size"] = 0
                                app["downloadURL"] = ""
            
            # Push updated source.json to GitHub
            github_pushed = False
            placeholder_added = False
            
            if updated_source:
                # Check if repo is now empty and add placeholder if needed
                if is_repo_empty(source):
                    source = get_placeholder_source()
                    placeholder_added = True
                    logger.info("Repo is now empty, adding placeholder app")
                
                await save_source_json(source)
                if GITHUB_TOKEN:
                    try:
                        commit_msg = f"Remove version {version_to_delete}"
                        if placeholder_added:
                            commit_msg += " and restore placeholder"
                        github_pushed = await push_file_to_github(
                            SOURCE_JSON_PATH,
                            "repo/esign/source.json",
                            commit_msg
                        )
                    except Exception as e:
                        logger.error(f"Failed to push source.json to GitHub: {e}")
            
            release_status = "✅" if release_deleted else "⚠️ (not found)"
            source_status = "✅ (pushed to GitHub)" if github_pushed else "⚠️ (local only)" if updated_source else "⚠️"
            
            placeholder_text = "\n📦 **Placeholder app restored** (repo was empty)" if placeholder_added else ""
            
            await callback.message.edit_text(
                f"✅ **GitHub Version Deleted**\n\n"
                f"🗑️ Deleted version: `{version_to_delete}`\n"
                f"🌐 **GitHub release:** {release_status}\n"
                f"📄 **Source updated:** {source_status}{placeholder_text}",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.clear()
            await callback.answer()
            return
        
        # Handle untracked files differently
        if is_untracked and filename_to_delete:
            # Delete untracked file directly
            if filename_to_delete == "soundcloud.ipa":
                file_path = ESIGN_PATH / filename_to_delete
            else:
                file_path = VERSIONS_PATH / filename_to_delete
            
            if file_path.exists():
                try:
                    file_path.unlink()
                    logger.info(f"Deleted untracked file: {filename_to_delete}")
                    
                    # Also update source.json to ensure it's clean
                    # Check if this file might be referenced in source.json by URL
                    source = await load_source_json()
                    updated_source = False
                    
                    # #region agent log
                    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1118", "message": "Checking source.json for untracked file deletion", "data": {"filename": filename_to_delete, "version": version_to_delete}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                    # #endregion
                    
                    if source.get("apps"):
                        for app in source["apps"]:
                            # Check if downloadURL points to this file or if version matches
                            download_url = app.get("downloadURL", "")
                            app_version = app.get("version", "")
                            
                            # Check if this version or file is referenced
                            if (filename_to_delete in download_url or 
                                "soundcloud.ipa" in download_url or
                                version_to_delete in app_version or
                                version_to_delete in download_url):
                                # This might be referencing the deleted file
                                # Remove it from versions array if it matches
                                if app.get("versions"):
                                    # Try to find and remove any version that might match
                                    original_count = len(app["versions"])
                                    app["versions"] = [
                                        v for v in app["versions"] 
                                        if (filename_to_delete not in v.get("downloadURL", "") and
                                            version_to_delete != v.get("version", ""))
                                    ]
                                    if len(app["versions"]) < original_count:
                                        updated_source = True
                                        
                                        # #region agent log
                                        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1140", "message": "Removed version from source.json", "data": {"removed_count": original_count - len(app["versions"]), "remaining_count": len(app["versions"])}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                                        # #endregion
                                        
                                        # Update app-level fields if needed
                                        if app.get("versions") and len(app["versions"]) > 0:
                                            versions_sorted = sorted(app["versions"], key=lambda x: x.get("date", ""), reverse=True)
                                            latest = versions_sorted[0]
                                            app["version"] = latest.get("version", "")
                                            app["versionDate"] = latest.get("date", "")
                                            app["size"] = latest.get("size", 0)
                                            app["downloadURL"] = latest.get("downloadURL", "")
                                        else:
                                            app["version"] = ""
                                            app["versionDate"] = ""
                                            app["size"] = 0
                                            app["downloadURL"] = ""
                                elif app_version == version_to_delete or filename_to_delete in download_url:
                                    # App-level fields reference this file/version
                                    updated_source = True
                                    app["version"] = ""
                                    app["versionDate"] = ""
                                    app["size"] = 0
                                    app["downloadURL"] = ""
                                    
                                    # #region agent log
                                    log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1160", "message": "Cleared app-level fields in source.json", "data": {}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                                    # #endregion
                    
                    # Push updated source.json to GitHub if it was modified
                    github_pushed = False
                    if updated_source:
                        # #region agent log
                        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1165", "message": "Saving updated source.json", "data": {"updated": True}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                        # #endregion
                        await save_source_json(source)
                        if GITHUB_TOKEN:
                            try:
                                github_pushed = await push_file_to_github(
                                    SOURCE_JSON_PATH,
                                    "repo/esign/source.json",
                                    f"Remove untracked file {filename_to_delete} from repository"
                                )
                                # #region agent log
                                log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1175", "message": "GitHub push result for untracked file deletion", "data": {"pushed": github_pushed}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                                # #endregion
                                if github_pushed:
                                    logger.info("Pushed updated source.json to GitHub after untracked file deletion")
                            except Exception as e:
                                logger.error(f"Failed to push source.json to GitHub: {e}")
                                # #region agent log
                                log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "C", "location": "bot.py:1182", "message": "GitHub push exception", "data": {"error": str(e)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
                                # #endregion
                    
                    push_status = "✅ (pushed to GitHub)" if github_pushed else "⚠️ (local only)" if updated_source else ""
                    success_text = (
                        f"✅ **Untracked File Deleted**\n\n"
                        f"🗑️ Deleted: `{filename_to_delete}`\n"
                        f"📱 Version: `{version_to_delete}`"
                    )
                    if push_status:
                        success_text += f"\n\n🔄 **Source updated:** {push_status}"
                    
                    await callback.message.edit_text(
                        success_text,
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await state.clear()
                    await callback.answer()
                    return
                except Exception as e:
                    logger.error(f"Error deleting untracked file {filename_to_delete}: {e}")
                    await callback.message.edit_text(
                        f"❌ **Error**\n\nFailed to delete file: `{str(e)}`",
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await state.clear()
                    await callback.answer()
                    return
            else:
                await callback.message.edit_text(
                    f"❌ **File not found**\n\nFile `{filename_to_delete}` does not exist.",
                    parse_mode=ParseMode.MARKDOWN,
                )
                await state.clear()
                await callback.answer()
                return
        
        # Handle tracked versions
        # Load version history
        history = await load_version_history()
        versions = history.get("versions", [])
        current_version = history.get("current_version", "")
        
        # Find and remove the version
        version_found = False
        deleted_filename = None
        for i, v in enumerate(versions):
            if v.get("version") == version_to_delete:
                version_found = True
                deleted_filename = v.get("filename", "")
                
                # Delete the IPA file if it exists
                if deleted_filename:
                    file_path = VERSIONS_PATH / deleted_filename
                    if file_path.exists():
                        try:
                            file_path.unlink()
                            logger.info(f"Deleted version file: {deleted_filename}")
                        except Exception as e:
                            logger.error(f"Error deleting file {deleted_filename}: {e}")
                
                # Remove from versions list
                versions.pop(i)
                break
        
        if not version_found:
            await callback.message.edit_text(
                f"❌ **Version not found**\n\nVersion `{version_to_delete}` was not found in the repository.",
                parse_mode=ParseMode.MARKDOWN,
            )
            await state.clear()
            await callback.answer()
            return
        
        # Update current_version if we deleted the current one
        if current_version == version_to_delete:
            if versions:
                # Set to the most recent version
                versions_sorted = sorted(versions, key=lambda x: x.get("date", ""), reverse=True)
                history["current_version"] = versions_sorted[0].get("version", "")
            else:
                history["current_version"] = ""
        
        # Update history
        history["versions"] = versions
        
        # Save updated history
        if await save_version_history(history):
            # Always update source.json to remove deleted version
            source = await load_source_json()
            if source.get("apps"):
                for app in source["apps"]:
                    # Remove deleted version from versions array
                    if app.get("versions"):
                        app["versions"] = [v for v in app.get("versions", []) if v.get("version") != version_to_delete]
                    
                    # Update app-level fields if we deleted the current version or if no versions left
                    if current_version == version_to_delete or not app.get("versions"):
                        if app.get("versions") and len(app["versions"]) > 0:
                            # Set to most recent remaining version
                            versions_sorted = sorted(app["versions"], key=lambda x: x.get("date", ""), reverse=True)
                            latest = versions_sorted[0]
                            app["version"] = latest.get("version", "")
                            app["versionDate"] = latest.get("date", "")
                            app["size"] = latest.get("size", 0)
                            app["downloadURL"] = latest.get("downloadURL", "")
                        else:
                            # No versions left
                            app["version"] = ""
                            app["versionDate"] = ""
                            app["size"] = 0
                            app["downloadURL"] = ""
            
            await save_source_json(source)
            
            # Push updated source.json to GitHub
            github_pushed = False
            if GITHUB_TOKEN:
                try:
                    github_pushed = await push_file_to_github(
                        SOURCE_JSON_PATH,
                        "repo/esign/source.json",
                        f"Remove version {version_to_delete} from repository"
                    )
                    if github_pushed:
                        logger.info("Pushed updated source.json to GitHub")
                except Exception as e:
                    logger.error(f"Failed to push source.json to GitHub: {e}")
            
            push_status = "✅ (pushed to GitHub)" if github_pushed else "⚠️ (local only)"
            await callback.message.edit_text(
                f"✅ **Version Deleted**\n\n"
                f"🗑️ Deleted version: `{version_to_delete}`\n"
                f"📚 Remaining versions: {len(versions)}\n"
                f"📱 Current version: `{history['current_version'] or 'None'}`\n\n"
                f"🔄 **Source updated:** {push_status}",
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info(f"Deleted version: {version_to_delete}")
        else:
            await callback.message.edit_text(
                "❌ **Deletion Failed**\n\nFailed to save updated version history.",
                parse_mode=ParseMode.MARKDOWN,
            )
        
        await state.clear()
        await callback.answer()
        
    except Exception as e:
        logger.error(f"Error deleting version: {e}")
        await callback.message.edit_text(
            f"❌ **Error**\n\nFailed to delete version: `{str(e)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        await state.clear()
        await callback.answer()


@router.callback_query(F.data == "confirm_upload")
async def callback_confirm(callback: CallbackQuery, state: FSMContext, telethon_client: TelegramClient, bot: Bot) -> None:
    """Confirm and start download."""
    if not is_owner(callback.from_user.id):
        await callback.answer("Access denied", show_alert=True)
        return

    user_id = callback.from_user.id
    if user_id not in pending_uploads:
        await callback.answer("No pending upload found", show_alert=True)
        return

    upload = pending_uploads[user_id]
    del pending_uploads[user_id]

    await callback.answer("Starting download...")

    # Update message to show progress
    progress_msg = await callback.message.edit_text(
        "📥 **Downloading IPA...**",
        parse_mode=ParseMode.MARKDOWN,
    )

    try:
        # Create progress tracker
        tracker = ProgressTracker(
            bot=bot,
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            total_size=upload["size"],
        )

        logger.info(f"Starting download: {upload['filename']}")

        # Always try Telethon first (much faster), fallback to aiogram
        download_success = False
        
        # Method 1: Try Telethon download from the message sent to bot
        try:
            logger.info("Attempting fast Telethon download...")
            
            # Get the message from our chat with the bot
            chat_id = upload["chat_id"]
            msg_id = upload["message_id"]
            
            # Get the message via Telethon
            telethon_msg = await telethon_client.get_messages(chat_id, ids=msg_id)
            
            if telethon_msg and telethon_msg.media:
                await telethon_client.download_media(
                    telethon_msg,
                    file=str(MAIN_IPA_PATH),
                    progress_callback=lambda current, total: asyncio.create_task(
                        tracker.update(current, total)
                    ),
                )
                logger.info("✅ Downloaded via Telethon (fast)")
                download_success = True
            else:
                logger.warning("Telethon could not access the message media")
                
        except Exception as telethon_err:
            logger.warning(f"Telethon download failed: {telethon_err}, trying aiogram...")
        
        # Method 2: Fallback to aiogram if Telethon failed
        if not download_success:
            logger.info("Downloading via aiogram (slower fallback)...")
            
            try:
                file = await bot.get_file(upload["file_id"])
                
                # Download with progress updates
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks
                
                async with aiofiles.open(MAIN_IPA_PATH, "wb") as f:
                    async for chunk in bot.download_file(file.file_path, chunk_size=chunk_size):
                        await f.write(chunk)
                        downloaded += len(chunk)
                        await tracker.update(downloaded, upload["size"])
                
                logger.info("Downloaded via aiogram")
                
            except Exception as aiogram_err:
                logger.error(f"Aiogram download failed: {aiogram_err}")
                raise Exception(f"Download failed: {aiogram_err}")

        # Verify file size
        if not MAIN_IPA_PATH.exists() or MAIN_IPA_PATH.stat().st_size == 0:
            raise Exception("Downloaded file is empty or missing")

        actual_size = MAIN_IPA_PATH.stat().st_size
        logger.info(f"Download complete: {format_size(actual_size)}")

        # Create versioned backup (non-blocking)
        version = upload["version"]
        backup_filename = f"soundcloud_{version}.ipa"
        backup_path = VERSIONS_PATH / backup_filename

        asyncio.create_task(copy_backup_async(MAIN_IPA_PATH, backup_path))
        logger.info(f"Creating backup: {backup_filename}")

        # Load current data
        changelog = await load_changelog()
        history = await load_version_history()
        now = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")

        # Upload to GitHub Releases
        await bot.edit_message_text(
            text="📤 **Uploading to GitHub Releases...**",
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            parse_mode=ParseMode.MARKDOWN,
        )
        
        # #region agent log
        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:1219", "message": "Before GitHub release upload", "data": {"version": version, "file_exists": MAIN_IPA_PATH.exists(), "file_size": MAIN_IPA_PATH.stat().st_size if MAIN_IPA_PATH.exists() else 0, "has_github_config": bool(GITHUB_TOKEN and GITHUB_OWNER and GITHUB_REPO)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
        # #endregion
        
        github_download_url = await upload_to_github_release(MAIN_IPA_PATH, version, changelog)
        
        # #region agent log
        log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "A", "location": "bot.py:1222", "message": "After GitHub release upload", "data": {"success": github_download_url is not None, "download_url": github_download_url or "None"}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
        # #endregion
        
        if github_download_url:
            download_url = github_download_url
            logger.info(f"Using GitHub URL: {download_url}")
        else:
            # Fallback to local URL if GitHub upload fails
            repo_url = get_repository_url()
            download_url = f"{repo_url}/soundcloud.ipa"
            logger.warning("GitHub upload failed, using local URL")

        # Update version history
        new_version_entry = {
            "version": version,
            "date": now,
            "size": actual_size,
            "filename": backup_filename,
            "changelog": changelog,
            "download_url": download_url,
        }

        # Check if version already exists, update if so
        existing_idx = None
        for i, v in enumerate(history["versions"]):
            if v["version"] == version:
                existing_idx = i
                break

        if existing_idx is not None:
            history["versions"][existing_idx] = new_version_entry
        else:
            history["versions"].append(new_version_entry)

        history["current_version"] = version

        # Cleanup old versions
        await cleanup_old_versions(history)

        # Save version history (atomic)
        await save_version_history(history)

        # Update source.json for ESign/Feather (correct Feather format with duplicate fields)
        now_iso = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Load existing source.json to preserve the description
        existing_source = await load_source_json()
        existing_description = ""
        if existing_source.get("apps") and len(existing_source["apps"]) > 0:
            existing_description = existing_source["apps"][0].get("localizedDescription", "")
        
        # Auto-update version in description
        # If description contains a version pattern, update it; otherwise use template
        import re
        version_pattern = r'\d+\.\d+(\.\d+)?'
        if existing_description and re.search(version_pattern, existing_description):
            # Replace existing version number with new one
            existing_description = re.sub(version_pattern, version, existing_description)
        else:
            # No description set, use default template with version
            existing_description = f"Updated to the latest version {version}"
        
        source = {
            "name": "woomc repo",
            "identifier": "xyz.woomc.repo",
            "iconURL": f"{REPO_URL}/esign/logo.png",
            "website": REPO_URL,
            "sourceURL": f"{REPO_URL}/esign/source.json",
            "apps": [
                {
                    "name": "SoundCloud",
                    "bundleIdentifier": "com.soundcloud.TouchApp",
                    "developerName": "woomc",
                    "iconURL": f"{REPO_URL}/esign/logo.png",
                    "localizedDescription": existing_description,
                    "subtitle": "Tweaked SoundCloud",
                    "tintColor": "FF5500",
                    "versions": [
                        {
                            "version": version,
                            "date": now_iso,
                            "size": actual_size,
                            "downloadURL": download_url,
                            "localizedDescription": changelog,
                        }
                    ],
                    "appPermissions": {},
                    "screenshotURLs": [],
                    # Duplicate fields required by Feather
                    "version": version,
                    "versionDate": now_iso,
                    "size": actual_size,
                    "downloadURL": download_url,
                }
            ],
            "news": [],
        }
        await save_source_json(source)

        # Push source.json to GitHub
        github_pushed = False
        if GITHUB_TOKEN:
            # #region agent log
            log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "bot.py:1297", "message": "Before pushing source.json to GitHub", "data": {"source_json_exists": SOURCE_JSON_PATH.exists(), "has_github_token": bool(GITHUB_TOKEN)}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
            # #endregion
            await bot.edit_message_text(
                text="🔄 **Pushing to GitHub...**",
                chat_id=callback.message.chat.id,
                message_id=progress_msg.message_id,
                parse_mode=ParseMode.MARKDOWN,
            )
            github_pushed = await push_file_to_github(
                SOURCE_JSON_PATH,
                "repo/esign/source.json",
                f"Update source.json for v{version}"
            )
            # #region agent log
            log_data = {"sessionId": "debug-session", "runId": "run1", "hypothesisId": "B", "location": "bot.py:1308", "message": "After pushing source.json to GitHub", "data": {"success": github_pushed}, "timestamp": int(datetime.now().timestamp() * 1000)}; f = open(r"c:\Users\schoo\Documents\Esign - FeatherRepo - Telegram bot\.cursor\debug.log", "a", encoding="utf-8"); f.write(json.dumps(log_data) + "\n"); f.close()
            # #endregion
            if github_pushed:
                logger.info("Pushed source.json to GitHub")

        # Success message
        if github_download_url and github_pushed:
            success_text = (
                "✅ **Upload Complete!**\n\n"
                f"📱 **Version:** `{version}`\n"
                f"💾 **Size:** {format_size(actual_size)}\n"
                f"📝 **Changelog:** _{changelog}_\n\n"
                "🌐 **Status:** Uploaded to GitHub ✅\n"
                "🚀 **GitHub Pages:** Auto-deploying... ✅\n\n"
                f"**Download URL:**\n`{download_url}`\n\n"
                f"**Repository:**\n{REPO_URL}"
            )
        else:
            repo_url = get_repository_url()
            success_text = (
                "✅ **Upload Complete!**\n\n"
                f"📱 **Version:** `{version}`\n"
                f"💾 **Size:** {format_size(actual_size)}\n"
                f"📝 **Changelog:** _{changelog}_\n\n"
                f"**Download URL:**\n`{download_url}`\n\n"
                f"**Repository Feed:**\n`{repo_url}/source.json`"
            )

        await bot.edit_message_text(
            text=success_text,
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            parse_mode=ParseMode.MARKDOWN,
        )

        logger.info(f"Upload complete: v{version}")
        await state.clear()

    except Exception as e:
        logger.error(f"Upload failed: {e}")
        await bot.edit_message_text(
            text=f"❌ **Upload Failed**\n\nError: `{str(e)}`",
            chat_id=callback.message.chat.id,
            message_id=progress_msg.message_id,
            parse_mode=ParseMode.MARKDOWN,
        )


# =============================================================================
# MAIN
# =============================================================================


async def main() -> None:
    """Main entry point."""
    # Validate configuration
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set in .env")
        sys.exit(1)
    if not OWNER_ID:
        print("❌ OWNER_ID not set in .env")
        sys.exit(1)
    if not API_ID or not API_HASH:
        print("❌ API_ID and API_HASH not set in .env")
        sys.exit(1)

    # Print startup banner
    print("\n" + "=" * 50)
    print("🚀 ESign Repository Bot".center(50))
    print("=" * 50)
    print(f"📂 Path: {ESIGN_PATH}")
    print(f"🌐 IP:   {get_local_ip()}")
    print(f"🔗 URL:  {get_repository_url()}")
    print("=" * 50 + "\n")

    # Setup directories and files
    if not await ensure_esign_setup():
        print("❌ Failed to setup directories. Check permissions.")
        sys.exit(1)
    
    logger.info("Directory setup complete")

    # Initialize Telethon client (global)
    global telethon_client
    telethon_client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    await telethon_client.start()
    logger.info("Telethon userbot connected")

    # Initialize Aiogram bot
    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)

    # Inject Telethon client into callback handlers
    @dp.callback_query.middleware()
    async def inject_telethon_middleware(handler, event, data):
        data["telethon_client"] = telethon_client
        data["bot"] = bot
        return await handler(event, data)

    # Register bot commands for autocomplete menu
    from aiogram.types import BotCommand
    commands = [
        BotCommand(command="start", description="Show help"),
        BotCommand(command="send", description="Upload a new IPA"),
        BotCommand(command="stop", description="Cancel current operation"),
        BotCommand(command="repoinfo", description="Repository status"),
        BotCommand(command="setchangelog", description="Set changelog text"),
        BotCommand(command="setdescription", description="Set app description"),
        BotCommand(command="deleteversion", description="Delete a version"),
        BotCommand(command="cleanreleases", description="Clean orphaned GitHub releases"),
        BotCommand(command="syncgithub", description="Force push source.json to GitHub"),
    ]
    await bot.set_my_commands(commands)

    print("✅ Bot is running! Press Ctrl+C to stop.\n")

    try:
        await dp.start_polling(bot)
    except asyncio.CancelledError:
        pass  # Graceful shutdown
    finally:
        print("\n⏹️  Shutting down...")
        await telethon_client.disconnect()
        await bot.session.close()
        print("👋 Bot stopped.\n")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass  # Suppress traceback on Ctrl+C
