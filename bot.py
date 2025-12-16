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

# Vercel URL (public repository URL)
VERCEL_URL = os.getenv("VERCEL_URL", "https://esign-olive.vercel.app")

# ESign paths (configurable via ESIGN_PATH env var)
DEFAULT_ESIGN_PATH = r"C:\inetpub\wwwroot\esign"
ESIGN_BASE_PATH = Path(os.getenv("ESIGN_PATH", DEFAULT_ESIGN_PATH))
VERSIONS_PATH = ESIGN_BASE_PATH / "versions"
MAIN_IPA_PATH = ESIGN_BASE_PATH / "soundcloud.ipa"
SOURCE_JSON_PATH = ESIGN_BASE_PATH / "source.json"
VERSION_HISTORY_PATH = ESIGN_BASE_PATH / "version_history.json"
CHANGELOG_PATH = ESIGN_BASE_PATH / "changelog.txt"

# Session file for Telethon
SESSION_NAME = "esignbot_session"

# Max versions to keep
MAX_VERSIONS = 20

# Executor for non-blocking file operations
executor = ThreadPoolExecutor(max_workers=2)

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


class ChangelogStates(StatesGroup):
    waiting_for_changelog = State()


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


# =============================================================================
# GITHUB API FUNCTIONS
# =============================================================================


async def upload_to_github_release(file_path: Path, version: str, changelog: str) -> Optional[str]:
    """Upload IPA to GitHub Releases and return the download URL."""
    if not GITHUB_TOKEN or not GITHUB_OWNER or not GITHUB_REPO:
        logger.error("GitHub configuration missing")
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
                if upload_resp.status == 201:
                    asset_data = await upload_resp.json()
                    download_url = asset_data["browser_download_url"]
                    logger.info(f"Uploaded IPA to GitHub: {download_url}")
                    return download_url
                else:
                    error = await upload_resp.text()
                    logger.error(f"Failed to upload asset: {error}")
                    return None
                    
    except Exception as e:
        logger.error(f"GitHub upload error: {e}")
        return None


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
                if resp.status in [200, 201]:
                    logger.info(f"Pushed {repo_path} to GitHub")
                    return True
                else:
                    error = await resp.text()
                    logger.error(f"Failed to push file: {error}")
                    return False
                    
    except Exception as e:
        logger.error(f"GitHub push error: {e}")
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
        ESIGN_BASE_PATH.mkdir(parents=True, exist_ok=True)
        
        # Create versions subfolder
        VERSIONS_PATH.mkdir(parents=True, exist_ok=True)
        
        # Test write permission
        test_file = ESIGN_BASE_PATH / ".write_test"
        try:
            test_file.touch()
            test_file.unlink()
        except PermissionError:
            logger.error(f"No write permission to {ESIGN_BASE_PATH}")
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
        
        logger.info(f"ESign setup complete at {ESIGN_BASE_PATH}")
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
            ],
        ]
    )


# =============================================================================
# COMMAND HANDLERS
# =============================================================================


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Show help and features."""
    if not is_owner(message.from_user.id):
        await message.answer("⛔ Access denied. This bot is private.")
        return

    help_text = (
        "🚀 **ESign Repository Bot**\n\n"
        "Manage your IPA repository for ESign/Feather apps.\n\n"
        "**How to use:**\n"
        "1️⃣ Forward an IPA file to this bot\n"
        "2️⃣ Review and confirm the upload\n"
        "3️⃣ Bot uploads to GitHub & updates Vercel\n\n"
        "**Commands:**\n"
        "• `/start` - Show this help\n"
        "• `/repoinfo` - Repository status\n"
        "• `/setchangelog [text]` - Set changelog\n\n"
        f"**Repository URL:**\n`{VERCEL_URL}`"
    )
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)


@router.message(Command("repoinfo"))
async def cmd_repoinfo(message: Message) -> None:
    """Display repository status."""
    if not is_owner(message.from_user.id):
        return

    history = await load_version_history()
    current_version = history.get("current_version", "")
    versions = history.get("versions", [])
    backup_count = len(versions)

    # Check if main IPA exists
    ipa_exists = "✅" if MAIN_IPA_PATH.exists() else "❌"
    ipa_size = format_size(MAIN_IPA_PATH.stat().st_size) if MAIN_IPA_PATH.exists() else "N/A"

    # Get GitHub download URL if available
    if current_version and GITHUB_OWNER and GITHUB_REPO:
        download_url = get_github_download_url(current_version)
    else:
        download_url = "No version uploaded yet"

    text = (
        "📦 **Repository Status**\n\n"
        f"📱 **Current Version:** `{current_version or 'None'}`\n"
        f"💾 **Local File:** {ipa_exists} ({ipa_size})\n"
        f"📚 **Backups:** {backup_count} versions\n\n"
        f"🌐 **Public Repository:**\n`{VERCEL_URL}`\n\n"
        f"📥 **GitHub Downloads:**\n`{download_url}`"
    )
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


# =============================================================================
# DOCUMENT HANDLER
# =============================================================================


@router.message(F.document)
async def handle_document(message: Message) -> None:
    """Handle forwarded IPA files."""
    if not is_owner(message.from_user.id):
        return

    doc = message.document
    filename = doc.file_name or ""

    # Check if it's an IPA file
    if not filename.lower().endswith(".ipa"):
        return

    logger.info(f"IPA detected: {filename}")

    # Extract version
    version = extract_version_from_filename(filename)
    size = doc.file_size
    changelog = await load_changelog()

    # Store pending upload info
    pending_uploads[message.from_user.id] = {
        "file_id": doc.file_id,
        "filename": filename,
        "version": version,
        "size": size,
        "message_id": message.message_id,
        "chat_id": message.chat.id,
    }

    # Show confirmation
    text = (
        "📦 **Detected IPA File**\n\n"
        f"📄 **Filename:** `{filename}`\n"
        f"🏷️ **Version:** `{version}`\n"
        f"💾 **Size:** {format_size(size)}\n\n"
        f"📝 **Changelog:**\n_{changelog}_\n\n"
        "Press **Confirm** to upload or **Edit Changelog** to modify."
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
async def callback_cancel(callback: CallbackQuery) -> None:
    """Cancel pending upload."""
    if not is_owner(callback.from_user.id):
        return

    if callback.from_user.id in pending_uploads:
        del pending_uploads[callback.from_user.id]

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


@router.callback_query(F.data == "confirm_upload")
async def callback_confirm(callback: CallbackQuery, telethon_client: TelegramClient, bot: Bot) -> None:
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
        "📥 **Preparing download...**",
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

        # Download using Telethon userbot (fast!)
        logger.info(f"Starting download: {upload['filename']}")

        # Get the message with the file via Telethon
        telethon_message = await telethon_client.get_messages(
            upload["chat_id"],
            ids=upload["message_id"],
        )

        if not telethon_message or not telethon_message.document:
            raise Exception("Could not find the file message")

        # Download to main IPA path
        await telethon_client.download_media(
            telethon_message,
            file=str(MAIN_IPA_PATH),
            progress_callback=lambda current, total: asyncio.create_task(
                tracker.update(current, total)
            ),
        )

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
        
        github_download_url = await upload_to_github_release(MAIN_IPA_PATH, version, changelog)
        
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

        # Update source.json for ESign/Feather
        source = {
            "apps": [
                {
                    "name": "SoundCloud",
                    "bundleIdentifier": "com.soundcloud.TouchApp",
                    "version": version,
                    "versionDate": now,
                    "downloadURL": download_url,
                    "size": actual_size,
                    "category": "Music",
                }
            ]
        }
        await save_source_json(source)

        # Push source.json to GitHub (triggers Vercel redeploy)
        github_pushed = False
        if GITHUB_TOKEN:
            await bot.edit_message_text(
                text="🔄 **Pushing to GitHub...**",
                chat_id=callback.message.chat.id,
                message_id=progress_msg.message_id,
                parse_mode=ParseMode.MARKDOWN,
            )
            github_pushed = await push_file_to_github(
                SOURCE_JSON_PATH,
                "esign/source.json",
                f"Update source.json for v{version}"
            )
            if github_pushed:
                logger.info("Pushed source.json to GitHub - Vercel will auto-deploy")

        # Success message
        if github_download_url and github_pushed:
            success_text = (
                "✅ **Upload Complete!**\n\n"
                f"📱 **Version:** `{version}`\n"
                f"💾 **Size:** {format_size(actual_size)}\n"
                f"📝 **Changelog:** _{changelog}_\n\n"
                "🌐 **Status:** Uploaded to GitHub ✅\n"
                "🚀 **Vercel:** Auto-deploying... ✅\n\n"
                f"**Download URL:**\n`{download_url}`\n\n"
                f"**Repository:**\nhttps://esign-olive.vercel.app"
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
    print(f"📂 Path: {ESIGN_BASE_PATH}")
    print(f"🌐 IP:   {get_local_ip()}")
    print(f"🔗 URL:  {get_repository_url()}")
    print("=" * 50 + "\n")

    # Setup directories and files
    if not await ensure_esign_setup():
        print("❌ Failed to setup directories. Check permissions.")
        sys.exit(1)
    
    logger.info("Directory setup complete")

    # Initialize Telethon client
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
