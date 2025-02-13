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
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
from datetime import datetime
import requests.utils as requests_utils

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

# Supported file types
DOCUMENT_EXTS = ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt']
IMAGE_EXTS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp']
ALLOWED_EXTS = DOCUMENT_EXTS + IMAGE_EXTS

def is_authorized_user(user_id):
    sudo_users = load_sudo_users()
    return user_id == OWNER_ID or user_id in sudo_users

def is_authorized_channel(channel_id):
    authorized_channels = load_channels()
    return channel_id in authorized_channels

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

def get_domain(url):
    parsed_uri = urlparse(url)
    return f"{parsed_uri.netloc}"

def load_user_data():
    try:
        with open(USER_DATA_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_user_data(user_data):
    with open(USER_DATA_FILE, 'w') as f:
        json.dump(user_data, f, indent=4)

def fetch_url_content(url):
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.error(f"Error fetching {url}: {e}")
        return None

async def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', '_', name).strip()

async def download_file(url, custom_name=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                response.raise_for_status()
                
                # Determine filename
                if custom_name:
                    base_name = await sanitize_filename(custom_name)
                else:
                    base_name = await sanitize_filename(os.path.basename(urlparse(url).path))
                
                # Get extension
                content_type = response.headers.get('Content-Type', '')
                ext = os.path.splitext(urlparse(url).path)[1].lower() or \
                      ('.jpg' if 'image/jpeg' in content_type else
                       '.png' if 'image/png' in content_type else
                       '.gif' if 'image/gif' in content_type else
                       '.pdf' if 'application/pdf' in content_type else
                       '.docx' if 'vnd.openxmlformats' in content_type else '')
                
                filename = f"{base_name}{ext}"
                
                # Download and save
                async with aiofiles.open(filename, 'wb') as f:
                    await f.write(await response.read())
                    return filename
    except Exception as e:
        logger.error(f"Download error {url}: {e}")
        return None

def extract_files(html_content, base_url):
    soup = BeautifulSoup(html_content, 'lxml')
    files = []

    # Extract documents from links
    for link in soup.find_all('a', href=True):
        href = link['href']
        encoded_href = requests_utils.requote_uri(href)
        absolute_url = urljoin(base_url, encoded_href)
        link_text = link.text.strip()

        if any(absolute_url.lower().endswith(tuple(ALLOWED_EXTS))):
            if not link_text:
                filename = os.path.basename(absolute_url)
                link_text = os.path.splitext(filename)[0]
            file_type = 'document' if any(absolute_url.lower().endswith(tuple(DOCUMENT_EXTS))) else 'image'
            files.append({
                'name': link_text,
                'url': absolute_url,
                'type': file_type
            })

    # Extract images from img tags
    for img in soup.find_all('img', src=True):
        src = img['src']
        absolute_url = urljoin(base_url, src)
        alt_text = img.get('alt', '').strip()

        if any(absolute_url.lower().endswith(tuple(IMAGE_EXTS))):
            name = alt_text or os.path.splitext(os.path.basename(absolute_url))[0]
            files.append({
                'name': name,
                'url': absolute_url,
                'type': 'image'
            })

    return list({f['url']: f for f in files}.values())

async def create_document_file(url, files):
    domain = get_domain(url)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{domain}_files_{timestamp}.txt"

    with open(filename, 'w', encoding='utf-8') as f:
        for file in files:
            f.write(f"{file['type'].upper()}: {file['name']} {file['url']}\n\n")
    
    if os.path.getsize(filename) > MAX_FILE_SIZE:
        os.remove(filename)
        return None
    return filename

async def check_website_updates(client):
    user_data = load_user_data()
    for user_id, data in user_data.items():
        for url_info in data['tracked_urls']:
            url = url_info['url']
            stored_hash = url_info['hash']
            stored_files = url_info['files']

            current_content = fetch_url_content(url)
            if not current_content:
                continue

            current_hash = hashlib.sha256(current_content.encode()).hexdigest()
            current_files = extract_files(current_content, url)

            if current_hash != stored_hash:
                try:
                    await client.send_message(
                        chat_id=user_id,
                        text=f"🚨 Website Updated: {url}"
                    )
                except Exception as e:
                    logger.error(f"Update send error: {e}")

                new_files = [f for f in current_files if f not in stored_files]

                if new_files:
                    # Send TXT summary
                    try:
                        txt_file = await create_document_file(url, new_files)
                        if txt_file:
                            await client.send_document(
                                chat_id=user_id,
                                document=txt_file,
                                caption=f"📄 New Files List ({len(new_files)})"
                            )
                            os.remove(txt_file)
                    except Exception as e:
                        logger.error(f"TXT send error: {e}")

                    # Send individual files
                    for file in new_files:
                        filename = None
                        try:
                            filename = await download_file(file['url'], file['name'])
                            if not filename:
                                raise Exception("Download failed")

                            await client.send_document(
                                chat_id=user_id,
                                document=filename,
                                caption=f"🆕 New {file['type'].capitalize()}:\n{file['name']}\n{file['url']}"
                            )
                            
                            # Update stored data
                            stored_files.append(file)
                            url_info['files'] = stored_files
                            url_info['hash'] = current_hash

                        except Exception as e:
                            await client.send_message(
                                chat_id=user_id,
                                text=f"❌ Error sending {file['name']}: {str(e)}"
                            )
                        finally:
                            if filename and os.path.exists(filename):
                                os.remove(filename)

                    save_user_data(user_data)

async def start(client, message):
    if message.chat.type == "private":
        if not is_authorized_user(message.from_user.id):
            await message.reply_text("❌ Unauthorized access.")
            return
    elif message.chat.type == "channel":
        if not is_authorized_channel(message.chat.id):
            await message.reply_text("❌ Unauthorized channel.")
            return
    else:
        await message.reply_text("❌ Command not allowed here.")
        return

    await message.reply_text(
        "🌐 Website Tracker Bot\n\n"
        "Commands:\n"
        "/track <url> - Start tracking website\n"
        "/untrack <url> - Stop tracking\n"
        "/list - Show tracked websites\n"
        "/documents <url> - Get files list\n"
        "/help - Show help"
    )

async def track(client, message):
    # Authorization check
    if message.chat.type == "private":
        if not is_authorized_user(message.from_user.id):
            await message.reply_text("❌ Unauthorized.")
            return
    elif message.chat.type == "channel":
        if not is_authorized_channel(message.chat.id):
            await message.reply_text("❌ Unauthorized channel.")
            return
    else:
        await message.reply_text("❌ Command not allowed here.")
        return

    user_id = str(message.from_user.id or message.chat.id)
    url = ' '.join(message.command[1:]).strip()

    if not url.startswith(('http://', 'https://')):
        await message.reply_text("⚠ Invalid URL format.")
        return

    user_data = load_user_data()
    if user_id not in user_data:
        user_data[user_id] = {'tracked_urls': []}

    if any(u['url'] == url for u in user_data[user_id]['tracked_urls']):
        await message.reply_text("⚠ Already tracking this URL.")
        return

    content = fetch_url_content(url)
    if not content:
        await message.reply_text("❌ Failed to access URL.")
        return

    current_hash = hashlib.sha256(content.encode()).hexdigest()
    current_files = extract_files(content, url)

    user_data[user_id]['tracked_urls'].append({
        'url': url,
        'hash': current_hash,
        'files': current_files
    })
    save_user_data(user_data)

    # Send initial files
    if current_files:
        await message.reply_text(f"⬇️ Found {len(current_files)} files...")
        for file in current_files:
            filename = await download_file(file['url'], file['name'])
            if filename:
                try:
                    await client.send_document(
                        chat_id=user_id,
                        document=filename,
                        caption=f"📁 {file['type'].capitalize()}: {file['name']}\n🔗 {file['url']}"
                    )
                finally:
                    if os.path.exists(filename):
                        os.remove(filename)
            else:
                await message.reply_text(f"❌ Failed to download {file['name']}")

    await message.reply_text(f"✅ Tracking started: {url}\nFiles found: {len(current_files)}")

async def untrack(client, message):
    # Authorization check similar to track function
    
    user_id = str(message.from_user.id or message.chat.id)
    url = ' '.join(message.command[1:]).strip()

    user_data = load_user_data()
    if user_id not in user_data:
        await message.reply_text("❌ No tracked URLs.")
        return

    original_count = len(user_data[user_id]['tracked_urls'])
    user_data[user_id]['tracked_urls'] = [
        u for u in user_data[user_id]['tracked_urls']
        if u['url'] != url
    ]

    if len(user_data[user_id]['tracked_urls']) < original_count:
        save_user_data(user_data)
        await message.reply_text(f"✅ Stopped tracking: {url}")
    else:
        await message.reply_text("❌ URL not found")

async def list_urls(client, message):
    # Authorization check
    
    user_id = str(message.from_user.id or message.chat.id)
    user_data = load_user_data()

    if user_id not in user_data or not user_data[user_id]['tracked_urls']:
        await message.reply_text("📭 No tracked URLs")
        return

    urls = "\n".join([u['url'] for u in user_data[user_id]['tracked_urls']])
    await message.reply_text(f"📜 Tracked URLs:\n\n{urls}")

async def list_documents(client, message):
    # Authorization check
    
    user_id = str(message.from_user.id or message.chat.id)
    url = ' '.join(message.command[1:]).strip()

    user_data = load_user_data()
    if user_id not in user_data or not user_data[user_id]['tracked_urls']:
        await message.reply_text("❌ No tracked URLs")
        return

    url_info = next((u for u in user_data[user_id]['tracked_urls'] if u['url'] == url), None)
    if not url_info:
        await message.reply_text("❌ URL not tracked")
        return

    files = url_info.get('files', [])
    if not files:
        await message.reply_text(f"ℹ️ No files found at {url}")
    else:
        try:
            txt_file = await create_document_file(url, files)
            await client.send_document(
                chat_id=user_id,
                document=txt_file,
                caption=f"📑 Files at {url} ({len(files)})"
            )
            os.remove(txt_file)
        except Exception as e:
            logger.error(f"Document list error: {e}")
            await message.reply_text("❌ Error sending file list")

# Add other management commands (addchannel, removesudo etc.) as needed


async def add_channel(client, message):
    """Handle /addchannel command (owner only)"""
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Only the owner can add authorized channels.")
            return

        if len(message.command) < 2:
            await message.reply_text("⚠️ Usage: /addchannel <channel_id>")
            return

        try:
            channel_id = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid channel ID. Must be an integer.")
            return

        authorized_channels = load_channels()

        if channel_id in authorized_channels:
            await message.reply_text("❌ This channel is already authorized.")
            return

        authorized_channels.append(channel_id)
        save_channels(authorized_channels)
        await message.reply_text(f"✅ Channel {channel_id} has been authorized.")

    except Exception as e:
        logger.error(f"Error in add_channel: {e}")
        await message.reply_text("❌ An error occurred while processing the command.")

async def remove_channel(client, message):
    """Handle /removechannel command (owner only)"""
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Only the owner can remove authorized channels.")
            return

        if len(message.command) < 2:
            await message.reply_text("⚠️ Usage: /removechannel <channel_id>")
            return

        try:
            channel_id = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid channel ID. Must be an integer.")
            return

        authorized_channels = load_channels()

        if channel_id not in authorized_channels:
            await message.reply_text("❌ This channel is not authorized.")
            return

        authorized_channels.remove(channel_id)
        save_channels(authorized_channels)
        await message.reply_text(f"❎ Channel {channel_id} has been removed from authorized channels.")

    except Exception as e:
        logger.error(f"Error in remove_channel: {e}")
        await message.reply_text("❌ An error occurred while processing the command.")

async def add_sudo_user(client, message):
    """Handle /addsudo command (owner only)"""
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Only the owner can add sudo users.")
            return

        if len(message.command) < 2:
            await message.reply_text("⚠️ Usage: /addsudo <user_id>")
            return

        try:
            sudo_user_id = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid user ID. Must be an integer.")
            return

        sudo_users = load_sudo_users()

        if sudo_user_id in sudo_users:
            await message.reply_text("❌ This user is already a sudo user.")
            return

        sudo_users.append(sudo_user_id)
        save_sudo_users(sudo_users)
        await message.reply_text(f"✅ User {sudo_user_id} has been added as a sudo user.")

    except Exception as e:
        logger.error(f"Error in add_sudo_user: {e}")
        await message.reply_text("❌ An error occurred while processing the command.")

async def remove_sudo_user(client, message):
    """Handle /removesudo command (owner only)"""
    try:
        if message.from_user.id != OWNER_ID:
            await message.reply_text("❌ Only the owner can remove sudo users.")
            return

        if len(message.command) < 2:
            await message.reply_text("⚠️ Usage: /removesudo <user_id>")
            return

        try:
            sudo_user_id = int(message.command[1])
        except ValueError:
            await message.reply_text("❌ Invalid user ID. Must be an integer.")
            return

        sudo_users = load_sudo_users()

        if sudo_user_id not in sudo_users:
            await message.reply_text("❌ This user is not a sudo user.")
            return

        sudo_users.remove(sudo_user_id)
        save_sudo_users(sudo_users)
        await message.reply_text(f"❎ User {sudo_user_id} has been removed from sudo users.")

    except Exception as e:
        logger.error(f"Error in remove_sudo_user: {e}")
        await message.reply_text("❌ An error occurred while processing the command.")

import asyncio
from pyrogram import idle

def main():
    app = Client(
        "my_bot",
        api_id=os.getenv("API_ID"),
        api_hash=os.getenv("API_HASH"),
        bot_token=os.getenv("BOT_TOKEN")
    )

    handlers = [
        MessageHandler(start, filters.command("start")),
        MessageHandler(track, filters.command("track")),
        MessageHandler(untrack, filters.command("untrack")),
        MessageHandler(list_urls, filters.command("list")),
        MessageHandler(list_documents, filters.command("documents")),
        MessageHandler(add_channel, filters.command("addchannel") & filters.private),
        MessageHandler(remove_channel, filters.command("removechannel") & filters.private),
        MessageHandler(add_sudo_user, filters.command("addsudo") & filters.private),
        MessageHandler(remove_sudo_user, filters.command("removesudo") & filters.private)
             # Add other handlers
    ]

    for handler in handlers:
        app.add_handler(handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_website_updates, 'interval', minutes=CHECK_INTERVAL, args=[app])

    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main(app, scheduler))

async def async_main(app, scheduler):
    scheduler.start()

    try:
        await app.start()
        await idle()
        await app.stop()
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")

if __name__ == '__main__':
    main()

def main():
    app = Client(
        "my_bot",
        api_id=os.getenv("API_ID"),
        api_hash=os.getenv("API_HASH"),
        bot_token=os.getenv("BOT_TOKEN")
    )

    handlers = [
        MessageHandler(start, filters.command("start")),
        MessageHandler(track, filters.command("track")),
        MessageHandler(untrack, filters.command("untrack")),
        MessageHandler(list_urls, filters.command("list")),
        MessageHandler(list_documents, filters.command("documents")),
        MessageHandler(add_channel, filters.command("addchannel") & filters.private),
        MessageHandler(remove_channel, filters.command("removechannel") & filters.private),
        MessageHandler(add_sudo_user, filters.command("addsudo") & filters.private),
        MessageHandler(remove_sudo_user, filters.command("removesudo") & filters.private)
             # Add other handlers
    ]

    for handler in handlers:
        app.add_handler(handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(check_website_updates, 'interval', minutes=CHECK_INTERVAL, args=[app])

    loop = asyncio.get_event_loop()
    loop.run_until_complete(async_main(app, scheduler))

async def async_main(app, scheduler):
    scheduler.start()

    try:
        await app.start()
        await idle()
        await app.stop()
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")

if __name__ == '__main__':
    main()

