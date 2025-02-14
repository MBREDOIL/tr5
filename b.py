import logging
import re
import aiohttp
import aiofiles
import hashlib
import json
import os
import requests
from pyrogram import Client, filters
from pyrogram.handlers import MessageHandler
from pyrogram.enums import ChatType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.combining import AndTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
import pytz

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Configuration
USER_DATA_FILE = 'user_data.json'
CHANNELS_FILE = 'authorized_channels.json'
SUDO_USERS_FILE = 'sudo_users.json'
OWNER_ID = 6556141430
MAX_FILE_SIZE = 45 * 1024 * 1024  # 45MB
CHECK_INTERVAL = 30  # Minutes
DEFAULT_TZ = pytz.timezone("Asia/Kolkata")

# Supported file types
DOCUMENT_EXTS = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt']
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']
AUDIO_EXTS = ['.mp3', '.wav', '.ogg']
VIDEO_EXTS = ['.mp4', '.mov', '.avi', '.mkv']
ALLOWED_EXTS = DOCUMENT_EXTS + IMAGE_EXTS + AUDIO_EXTS + VIDEO_EXTS

# Scheduler configuration
jobstores = {
    'default': SQLAlchemyJobStore(url='sqlite:///jobs.sqlite')
}
job_defaults = {
    'misfire_grace_time': 3600,  # 1 hour grace period
    'coalesce': True,
    'max_instances': 1
}

scheduler = AsyncIOScheduler(jobstores=jobstores, job_defaults=job_defaults, timezone=DEFAULT_TZ)

def load_channels():
    try:
        with open(CHANNELS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_channels(channels):
    with open(CHANNELS_FILE, 'w') as f:
        json.dump(channels, f, indent=4)

def load_sudo_users():
    try:
        with open(SUDO_USERS_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_sudo_users(sudo_users):
    with open(SUDO_USERS_FILE, 'w') as f:
        json.dump(sudo_users, f, indent=4)

def load_user_data():
    try:
        with open(USER_DATA_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_data(user_data):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)

async def download_file(url, custom_name=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                
                content_type = response.headers.get('Content-Type', '')
                ext = os.path.splitext(urlparse(url).path)[1].lower()
                
                # Determine file type
                if not ext:
                    if 'audio' in content_type:
                        ext = '.mp3'
                    elif 'video' in content_type:
                        ext = '.mp4'
                    elif 'image' in content_type:
                        ext = '.jpg'
                    elif 'pdf' in content_type:
                        ext = '.pdf'
                    else:
                        ext = '.bin'

                base_name = custom_name or os.path.splitext(os.path.basename(urlparse(url).path))[0]
                safe_name = re.sub(r'[\\/*?:"<>|]', '_', base_name).strip()
                filename = f"{safe_name}{ext}"
                
                async with aiofiles.open(filename, 'wb') as f:
                    await f.write(await response.read())
                    return filename
    except Exception as e:
        logger.error(f"Download error {url}: {e}")
        return None

def extract_files(html_content, base_url):
    soup = BeautifulSoup(html_content, 'lxml')
    files = []

    # Extract all media elements
    for tag in soup.find_all(['a', 'img', 'audio', 'video', 'source']):
        url = None
        name = ''
        file_type = 'document'
        
        if tag.name == 'a' and tag.get('href'):
            url = urljoin(base_url, tag['href'])
            name = tag.text.strip()
        elif tag.name in ['img', 'audio', 'video', 'source'] and tag.get('src'):
            url = urljoin(base_url, tag['src'])
            name = tag.get('alt', tag.get('title', ''))
        
        if url and any(url.lower().endswith(tuple(ALLOWED_EXTS)):
            # Determine file type
            if url.lower().endswith(tuple(IMAGE_EXTS)):
                file_type = 'image'
            elif url.lower().endswith(tuple(AUDIO_EXTS)):
                file_type = 'audio'
            elif url.lower().endswith(tuple(VIDEO_EXTS)):
                file_type = 'video'
            elif url.lower().endswith(tuple(DOCUMENT_EXTS)):
                file_type = 'document'
            
            if not name:
                name = os.path.splitext(os.path.basename(url))[0]
            
            files.append({
                'name': name,
                'url': url,
                'type': file_type
            })

    return list({f['url']: f for f in files}.values())

async def check_website_updates(client):
    user_data = load_user_data()
    for user_id, data in user_data.items():
        for url, info in data.get('tracked_urls', {}).items():
            try:
                # Existing checking logic with improved error handling
                content = fetch_url_content(url)
                if not content:
                    continue

                current_hash = hashlib.sha256(content.encode()).hexdigest()
                if current_hash != info.get('hash'):
                    await handle_website_update(client, user_id, url, content)
                    # Update hash after handling changes
                    info['hash'] = current_hash
                    save_user_data(user_data)
            except Exception as e:
                logger.error(f"Update check failed for {url}: {e}")

# Improved scheduler configuration
def schedule_job(url, user_id, interval, night_mode=False):
    trigger = IntervalTrigger(minutes=interval) 
    if night_mode:
        trigger = AndTrigger([
            trigger,
            CronTrigger(hour='6-22', timezone=DEFAULT_TZ)
        ])
    
    return scheduler.add_job(
        check_single_website,
        trigger=trigger,
        args=[client, url, user_id],
        replace_existing=True,
        id=f"{user_id}_{url_hash}",
        misfire_grace_time=3600
    )

# Add job missed listener
def job_missed(event):
    logger.warning(f"Job {event.job_id} missed by {event.scheduled_run_time}")
    # Reschedule missed job
    scheduler.modify_job(event.job_id, next_run_time=datetime.now(DEFAULT_TZ))

scheduler.add_listener(job_missed, EVENT_JOB_MISSED)

# Command handlers
async def track(client, message):
    try:
        # Existing track command logic with improved validation
        parts = message.command[1:]
        if len(parts) < 2:
            await message.reply_text("Usage: /track <url> <interval> [night]")
            return

        url = parts[0]
        interval = int(parts[1])
        night_mode = 'night' in parts[2:]

        job = schedule_job(url, message.chat.id, interval, night_mode)
        
        # Save to user data with new format
        user_data = load_user_data()
        user_data.setdefault(str(message.chat.id), {}).setdefault('tracked_urls', {})[url] = {
            'job_id': job.id,
            'interval': interval,
            'night_mode': night_mode,
            'last_checked': datetime.now(DEFAULT_TZ).isoformat()
        }
        save_user_data(user_data)

        await message.reply_text(
            f"✅ Tracking started for {url}\n"
            f"• Interval: {interval} minutes\n"
            f"• Night mode: {'ON' if night_mode else 'OFF'}"
        )
    except Exception as e:
        logger.error(f"Track command error: {e}")
        await message.reply_text("❌ Failed to start tracking")

# Other command handlers (untrack, list, etc.) with similar improvements

def main():
    app = Client(
        "my_bot",
        api_id=os.getenv("API_ID"),
        api_hash=os.getenv("API_HASH"),
        bot_token=os.getenv("BOT_TOKEN"),
        workers=3
    )

    handlers = [
        MessageHandler(start, filters.command("start")),
        MessageHandler(track, filters.command("track")),
        MessageHandler(untrack, filters.command("untrack")),
        MessageHandler(list_urls, filters.command("list")),
        # Add other handlers
    ]

    for handler in handlers:
        app.add_handler(handler)

    try:
        scheduler.start()
        app.run()
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")
    finally:
        scheduler.shutdown()

if __name__ == '__main__':
    main()