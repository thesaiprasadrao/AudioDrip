from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    BotCommand,
    BotCommandScopeDefault,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from telegram.constants import ChatAction
import yt_dlp
import os
import asyncio
import logging
import json
import requests
from io import BytesIO
import time
import random
import re
import shutil
import subprocess
try:
    # Optional .env support for local dev
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# Enable logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Bot token is loaded from environment
BOT_TOKEN = os.getenv("BOT_TOKEN")
_COOKIES_FILE = None
_COOKIES_ENV_B64 = os.getenv("YTDLP_COOKIES_B64")
if _COOKIES_ENV_B64:
    try:
        import base64
        cookies_bytes = base64.b64decode(_COOKIES_ENV_B64)
        cookies_path = "/tmp/yt_cookies.txt"
        with open(cookies_path, "wb") as cf:
            cf.write(cookies_bytes)
        _COOKIES_FILE = cookies_path
        logger.info("yt-dlp cookies loaded from YTDLP_COOKIES_B64")
    except Exception as e:
        logger.warning(f"Failed to load cookies from YTDLP_COOKIES_B64: {e}")
async def _retry_async(func, *args, retries=3, base_delay=1.0, jitter=0.5, **kwargs):
    """Generic async retry with exponential backoff and jitter"""
    attempt = 0
    while True:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            attempt += 1
            if attempt > retries:
                raise
            delay = base_delay * (2 ** (attempt - 1)) + random.uniform(0, jitter)
            logger.warning(f"Retrying after error: {e} (attempt {attempt}/{retries}) in {delay:.1f}s")
            await asyncio.sleep(delay)


def download_thumbnail(thumbnail_url, filename):
    """Download and save thumbnail image"""
    try:
        response = requests.get(thumbnail_url, timeout=10)
        if response.status_code == 200:
            with open(filename, 'wb') as f:
                f.write(response.content)
            return filename
        return None
    except Exception as e:
        logger.error(f"Thumbnail download error: {e}")
        return None

def _ensure_cache_dir():
    """Ensure cache directory exists for storing last audios."""
    cache_dir = os.path.join(os.getcwd(), 'cache')
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except Exception as e:
        logger.warning(f"Failed to create cache dir: {e}")
    return cache_dir

def _cleanup_cache(max_age_seconds: int = 2 * 60 * 60):
    """Delete cached files older than max_age_seconds."""
    cache_dir = _ensure_cache_dir()
    now = time.time()
    try:
        for name in os.listdir(cache_dir):
            path = os.path.join(cache_dir, name)
            try:
                if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                    os.remove(path)
            except Exception:
                pass
    except Exception:
        pass

def _sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = name.strip()
    return name or 'audio'

def _parse_timestamp_to_seconds(value: str) -> int:
    """Parse timestamps like ss, mm:ss, hh:mm:ss into total seconds. Accepts 1:02, 01:02:03, or integers.
    Raises ValueError if invalid."""
    value = value.strip()
    if not value:
        raise ValueError("Empty timestamp")
    # If numeric seconds
    if re.fullmatch(r"\d+", value):
        return int(value)
    # Support mm:ss or hh:mm:ss
    if re.fullmatch(r"\d{1,2}:\d{1,2}(:\d{1,2})?", value):
        parts = [int(p) for p in value.split(":")]
        if len(parts) == 2:
            m, s = parts
            if s >= 60 or m < 0 or s < 0:
                raise ValueError("Invalid mm:ss")
            return m * 60 + s
        elif len(parts) == 3:
            h, m, s = parts
            if m >= 60 or s >= 60 or h < 0 or m < 0 or s < 0:
                raise ValueError("Invalid hh:mm:ss")
            return h * 3600 + m * 60 + s
    # Support formats like 1m30s, 90s
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", value)
    if match and any(group is not None for group in match.groups()):
        h = int(match.group(1) or 0)
        m = int(match.group(2) or 0)
        s = int(match.group(3) or 0)
        return h * 3600 + m * 60 + s
    raise ValueError(f"Invalid timestamp format: {value}")

async def _ffmpeg_cut_audio(input_path: str, start_sec: int, end_sec: int, output_path: str) -> None:
    """Use ffmpeg to cut audio between start_sec and end_sec (re-encode for precision)."""
    if start_sec < 0 or end_sec <= start_sec:
        raise ValueError("End time must be greater than start time")
    duration = end_sec - start_sec
    cmd = [
        'ffmpeg', '-y',
        '-ss', str(start_sec),
        '-t', str(duration),
        '-i', input_path,
        '-vn',
        '-acodec', 'libmp3lame', '-b:a', '128k',
        output_path
    ]
    logger.info(f"Running ffmpeg: {' '.join(cmd)}")
    # Run ffmpeg in a thread to avoid blocking event loop
    def _run():
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors='ignore')[-5000:])
    await asyncio.to_thread(_run)

def search_songs(song_name, num_results=3):
    """Search for songs and return top results without downloading"""
    logger.info(f"🔍 Searching for: {song_name}")
    
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,  # We need full info
        'retries': 3,
        'socket_timeout': 20,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android']  # helps bypass some web-only checks
            }
        },
    }
    if _COOKIES_FILE:
        ydl_opts['cookiefile'] = _COOKIES_FILE
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Search for multiple results
            search_results = ydl.extract_info(
                f"ytsearch{num_results}:{song_name}", 
                download=False
            )
            
            if search_results and 'entries' in search_results:
                results = []
                for i, entry in enumerate(search_results['entries'][:num_results]):
                    if entry:
                        # Format duration
                        duration = entry.get('duration', 0)
                        if duration:
                            mins, secs = divmod(duration, 60)
                            duration_str = f"{mins}:{secs:02d}"
                        else:
                            duration_str = "Unknown"
                        
                        # Get the best thumbnail
                        thumbnails = entry.get('thumbnails', [])
                        thumbnail_url = None
                        if thumbnails:
                            # Try to get a medium-quality thumbnail
                            for thumb in reversed(thumbnails):
                                if thumb.get('width', 0) >= 320:
                                    thumbnail_url = thumb.get('url')
                                    break
                            if not thumbnail_url and thumbnails:
                                thumbnail_url = thumbnails[-1].get('url')
                        
                        results.append({
                            'index': i + 1,
                            'title': entry.get('title', 'Unknown Title'),
                            'uploader': entry.get('uploader', 'Unknown Artist'),
                            'duration': duration_str,
                            'url': entry.get('webpage_url', ''),
                            'id': entry.get('id', ''),
                            'view_count': entry.get('view_count', 0),
                            'thumbnail_url': thumbnail_url
                        })
                
                return results
            else:
                return []
                
    except Exception as e:
        logger.error(f"❌ Search error: {str(e)}")
        raise Exception(f"Search failed: {str(e)}")

def download_song_by_url(url):
    """Download a specific song by its URL"""
    logger.info(f"🎵 Starting download from URL: {url}")
    
    # Enhanced options for better results with dynamic filename
    ydl_opts = {
        'format': 'bestaudio[filesize<50M]/best[filesize<50M]',  # Prefer files under 50MB
        'outtmpl': '%(title)s.%(ext)s',  # Use actual song title as filename
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '128',  # Good balance of quality and size
        }],
        'quiet': True,  # Keep quiet for better UX
        'no_warnings': True,
        'extractaudio': True,
        'audioformat': 'mp3',
        'embed_subs': False,
        'writesubtitles': False,
        'writethumbnail': True,  # Download thumbnail
        'retries': 3,
        'fragment_retries': 3,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9'
        },
        'extractor_args': {
            'youtube': {
                'player_client': ['android']
            }
        },
    }
    if _COOKIES_FILE:
        ydl_opts['cookiefile'] = _COOKIES_FILE
    
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            logger.info("� Downloading selected song...")
            
            # Download the specific URL
            info = ydl.extract_info(url, download=True)
            
            if info:
                title = info.get('title', 'Unknown')
                duration = info.get('duration', 0)
                uploader = info.get('uploader', 'Unknown')
                
                # Clean the title for filename (remove invalid characters)
                import re
                clean_title = re.sub(r'[<>:"/\\|?*]', '', title)
                clean_title = clean_title.strip()
                
                # Create the expected filename
                expected_filename = f"{clean_title}.mp3"
                
                # Look for thumbnail file
                thumbnail_file = None
                import glob
                for ext in ['.jpg', '.jpeg', '.png', '.webp']:
                    thumb_pattern = f"{clean_title}{ext}"
                    if os.path.exists(thumb_pattern):
                        thumbnail_file = thumb_pattern
                        break
                
                # If no exact match, look for any image file
                if not thumbnail_file:
                    image_files = glob.glob("*.jpg") + glob.glob("*.jpeg") + glob.glob("*.png") + glob.glob("*.webp")
                    if image_files:
                        thumbnail_file = image_files[0]
                
                # Format duration
                if duration:
                    mins, secs = divmod(duration, 60)
                    duration_str = f"{mins}:{secs:02d}"
                else:
                    duration_str = "Unknown"
                
                logger.info(f"✅ Downloaded: {title} by {uploader} ({duration_str})")
                
                # Return the actual filename and song info
                return {
                    'filename': expected_filename,
                    'title': title,
                    'uploader': uploader,
                    'duration': duration_str,
                    'duration_seconds': int(duration) if isinstance(duration, (int, float)) else None,
                    'thumbnail': thumbnail_file
                }
            else:
                raise Exception("Failed to download the selected song")
                
    except Exception as e:
        logger.error(f"❌ Download error: {str(e)}")
        raise Exception(f"Failed to download: {str(e)}")

async def start_command(update, context):
    user_name = update.effective_user.first_name
    welcome_message = f"""
🎵 **Welcome {user_name}!** 🎵

I'm your personal **Music Download Bot**! �

**🎯 What I can do:**
• Download songs from YouTube
• Convert to high-quality MP3
• Send directly to your chat
• Search by song name or artist

**🎮 Available Commands:**
/start - Show this welcome message
/help - Get detailed help
/search <song name> - Search and download
/cut <start> <end> - Cut a part from the last audio
/stats - Bot statistics
/about - About this bot

**🚀 Quick Start:**
Just type any song name like:
• "Bohemian Rhapsody Queen"
• "Shape of You Ed Sheeran"
• "Blinding Lights Weeknd"

**💡 Pro Tips:**
• Add artist name for better results
• Files are limited to 50MB
• High quality 128kbps MP3 format
• Use /cut to extract a segment e.g., `/cut 0:30 1:15`

Ready to rock? 🎸 Send me a song name!
"""
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

async def help_command(update, context):
    help_text = """
🆘 **Help & Commands** 🆘

**📋 Available Commands:**
/start - Welcome message and quick start
/help - This help message
/search <song> - Search and download specific song
/cut <start> <end> - Cut last audio you received from the bot between timestamps
/stats - Show bot usage statistics
/about - Information about this bot

**🎵 How to Download Music:**
1️⃣ **Method 1:** Just type the song name
   Example: `Imagine Dragons Believer`

2️⃣ **Method 2:** Use /search command
   Example: `/search Taylor Swift Shake It Off`

**💡 Search Tips:**
• Include artist name for better results
• Use quotes for exact matches: "Hotel California"
• Try different variations if not found
• Popular songs work best

**⚡ Features:**
• 🎧 High-quality 128kbps MP3
• 📱 Mobile-friendly file sizes
• 🚀 Fast download & delivery
• 🔍 Smart YouTube search
• 🧹 Auto cleanup after sending
• ✂️ Cut audio by time range using /cut command

**✂️ Audio Cutter (/cut):**
Use `/cut <start> <end>` to trim the last song you got from me.
• Examples:
    - `/cut 30 75` (from 00:30 to 01:15)
    - `/cut 1:05 2:10` (mm:ss)
    - `/cut 0:45 3:00`
    - `/cut 1m 1m30s` (1:00 to 1:30)
• Supported formats: `ss`, `mm:ss`, `hh:mm:ss`, or `1h2m3s`
• File limit: 50MB for the clipped result

**⚠️ Limitations:**
• Maximum file size: 50MB
• YouTube content only
• No copyrighted content downloads
• Rate limited for fair usage

Need more help? Just ask! 🤗
"""
    await update.message.reply_text(help_text, parse_mode='Markdown')

async def stats_command(update, context):

    await update.message.reply_text("hey")

async def about_command(update, context):
    about_text = """
ℹ️ **About Music Bot** ℹ️

**🎵 Music Download Bot v2.0**
Your personal assistant for downloading music!

**👨‍💻 Developer:** @thesaiprasadrao
**🚀 Built with:**
• Python 3.13
• python-telegram-bot library
• yt-dlp (YouTube downloader)
• ffmpeg (audio processing)

**🌟 Features:**
• Smart music search
• High-quality audio conversion
• Fast & reliable downloads
• Clean user interface
• Regular updates & improvements

**📞 Support:**
• Having issues? Use /help
• Feature requests welcome!
• Report bugs via direct message


**🙏 Credits:**
• YouTube for music content
• FFmpeg team for audio processing
• yt-dlp developers
• Telegram Bot API

Made with ❤️ for music lovers!
"""
    await update.message.reply_text(about_text, parse_mode='Markdown')

async def search_command(update, context):
    if not context.args:
        await update.message.reply_text(
            "🔍 **Search Usage:**\n\n"
            "Use: `/search <song name>`\n\n"
            "**Examples:**\n"
            "• `/search Bohemian Rhapsody`\n"
            "• `/search Taylor Swift Love Story`\n"
            "• `/search Eminem Lose Yourself`\n\n"
            "Or just type the song name directly! 🎵",
            parse_mode='Markdown'
        )
        return
    
    query = ' '.join(context.args)
    await handle_music_request(update, context, query)

async def handle_music_request(update, context, query):
    user_name = update.effective_user.first_name
    
    # Send initial search message
    search_msg = await update.message.reply_text(
        f"🎵 Hey {user_name}! Searching for: **{query}**\n"
        "🔍 Looking through YouTube... ⏳",
        parse_mode='Markdown'
    )
    
    try:
        # Search for songs and get top 3 results
        results = search_songs(query, 3)
        
        if not results:
            await search_msg.edit_text(
                f"❌ **No results found for:** {query}\n\n"
                "💡 **Try:**\n"
                "• Different spelling\n"
                "• Include artist name\n"
                "• Use simpler search terms\n"
                "• Check for typos\n\n"
                "Search again with a different query! 🔍",
                parse_mode='Markdown'
            )
            return
        
        # Send each result as a separate message with thumbnail and button
        await search_msg.edit_text(
            f"🎵 **Found {len(results)} results for:** {query}\n"
            "Choose which one to download below! 👇",
            parse_mode='Markdown'
        )
        
        # Store search results for callback
        context.user_data['search_results'] = results
        context.user_data['search_query'] = query
        
        # Send each result as a photo with inline button
        for i, result in enumerate(results):
            # Format result info
            views = result['view_count']
            if views:
                if views >= 1000000:
                    view_text = f"{views/1000000:.1f}M views"
                elif views >= 1000:
                    view_text = f"{views/1000:.0f}K views"
                else:
                    view_text = f"{views} views"
            else:
                view_text = "Views not available"
            
            caption = f"**{result['index']}.** {result['title']}\n\n"
            caption += f"🎤 **Artist:** {result['uploader']}\n"
            caption += f"⏱️ **Duration:** {result['duration']}\n"
            caption += f"👁️ **Views:** {view_text}"
            
            # Create individual button for this result
            keyboard = [[InlineKeyboardButton(
                f"📥 Download This Song", 
                callback_data=f"download_{result['index']}_{result['id']}"
            )]]
            
            # Add cancel button to the last result
            if i == len(results) - 1:
                keyboard.append([InlineKeyboardButton("❌ Cancel All", callback_data="cancel")])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            # Try to send with thumbnail
            if result['thumbnail_url']:
                try:
                    await update.message.reply_photo(
                        photo=result['thumbnail_url'],
                        caption=caption,
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
                except Exception as e:
                    logger.error(f"Failed to send photo: {e}")
                    # Fallback to text message
                    await update.message.reply_text(
                        f"🖼️ [Thumbnail not available]\n\n{caption}",
                        parse_mode='Markdown',
                        reply_markup=reply_markup
                    )
            else:
                # Send as text if no thumbnail
                await update.message.reply_text(
                    f"🖼️ [No thumbnail]\n\n{caption}",
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
        
    except Exception as e:
        logger.error(f"Error in handle_music_request: {str(e)}")
        await search_msg.edit_text(
            f"❌ **Oops! Something went wrong** 😅\n\n"
            f"**Error:** {str(e)}\n\n"
            "💡 **Try:**\n"
            "• Different song name\n"
            "• Include artist name\n"
            "• Check spelling\n"
            "• Try again in a moment\n\n"
            "Need help? Use /help 🆘",
            parse_mode='Markdown'
        )

async def handle_download_callback(update, context):
    """Handle the download button callbacks"""
    query = update.callback_query
    await query.answer()  # Answer the callback query

    user_name = update.effective_user.first_name
    data = query.data

    # Determine chat id safely
    chat_id = None
    if query.message and getattr(query.message, 'chat', None):
        chat_id = query.message.chat.id
    else:
        # fallback to user id
        chat_id = update.effective_user.id

    if data == "cancel":
        # Send a new message instead of editing (since original might be a photo)
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ **Download cancelled**\n\nSend me another song name anytime! 🎵",
            parse_mode='Markdown'
        )
        return

    # Parse callback data
    try:
        parts = data.split('_')
        if len(parts) >= 3 and parts[0] == "download":
            result_index = int(parts[1]) - 1
            video_id = parts[2]

            # Get the selected result
            search_results = context.user_data.get('search_results', [])
            if result_index < len(search_results):
                selected = search_results[result_index]

                # Send new message to show download progress
                progress_msg = await _retry_async(
                    context.bot.send_message,
                    chat_id=chat_id,
                    text=f"🎵 **Downloading:** {selected['title']}\n"
                         f"🎤 Artist: {selected['uploader']}\n"
                         f"⏱️ Duration: {selected['duration']}\n\n"
                         "📥 Starting download... 🎧",
                    parse_mode='Markdown'
                )

                # Download the selected song
                try:
                    song_info = download_song_by_url(selected['url'])
                    song_file = song_info['filename']

                    # Check if file exists
                    if not os.path.exists(song_file):
                        import glob
                        mp3_files = glob.glob("*.mp3")
                        if mp3_files:
                            song_file = mp3_files[0]
                        else:
                            raise Exception("Downloaded file not found")

                    # Check file size
                    file_size = os.path.getsize(song_file)
                    file_size_mb = file_size / (1024*1024)

                    if file_size > 50 * 1024 * 1024:  # 50MB limit
                        await _retry_async(
                            progress_msg.edit_text,
                            f"❌ **File too large!** ({file_size_mb:.2f} MB)\n\n"
                            "The selected song exceeds Telegram's 50MB limit.\n"
                            "Try selecting a different version! 🔍",
                            parse_mode='Markdown'
                        )
                        os.remove(song_file)
                        return

                    # Update progress
                    await _retry_async(
                        progress_msg.edit_text,
                        f"🎵 **Almost ready!**\n"
                        f"📤 Uploading: **{song_info['title']}**\n"
                        f"📁 Size: {file_size_mb:.2f} MB\n"
                        "🎧 Sending to you now... ⚡",
                        parse_mode='Markdown'
                    )

                    # Attempt to send audio; validate thumbnail first
                    send_exception = None
                    sent_ok = False

                    # Try to open audio file
                    try:
                        audio_fp = open(song_file, 'rb')
                    except Exception as e:
                        raise Exception(f"Failed to open audio file: {e}")

                    # Prepare thumbnail if available and valid (JPEG and <=200KB)
                    thumb_fp = None
                    try:
                        thumb_path = song_info.get('thumbnail')
                        if thumb_path and os.path.exists(thumb_path):
                            ext = os.path.splitext(thumb_path)[1].lower()
                            size_ok = os.path.getsize(thumb_path) <= 200 * 1024
                            if ext in ('.jpg', '.jpeg') and size_ok:
                                thumb_fp = open(thumb_path, 'rb')
                            else:
                                logger.info(f"Thumbnail skipped (format/size): {thumb_path}")
                                thumb_fp = None
                    except Exception:
                        thumb_fp = None

                    # Show uploading action
                    try:
                        await _retry_async(context.bot.send_chat_action, chat_id=chat_id, action=ChatAction.UPLOAD_AUDIO)
                    except Exception:
                        pass

                    # Try sending as audio first
                    try:
                        # Build InputFile to ensure filename is preserved
                        audio_input = InputFile(audio_fp, filename=os.path.basename(song_file))
                        await _retry_async(
                            context.bot.send_audio,
                            chat_id=chat_id,
                            audio=audio_input,
                            thumbnail=thumb_fp,
                            title=song_info['title'],
                            performer=song_info['uploader'],
                            duration=song_info.get('duration_seconds') or None,
                            caption=(f"🎵 **{song_info['title']}**\n"
                                     f"🎤 Artist: {song_info['uploader']}\n"
                                     f"⏱️ Duration: {song_info['duration']}\n"
                                     f"📁 Size: {file_size_mb:.2f} MB\n"
                                     f"🎧 Quality: 128kbps MP3\n"
                                     f"👤 Requested by: {user_name}\n\n"
                                     "Enjoy your music! 🎶"),
                            parse_mode='Markdown'
                        )
                        sent_ok = True
                    except Exception as e:
                        send_exception = e
                        logger.error(f"send_audio failed: {e}")

                    # If send_audio failed, fallback to send_document
                    if not sent_ok and send_exception:
                        try:
                            # Rewind or reopen audio_fp
                            try:
                                audio_fp.close()
                            except:
                                pass
                            audio_fp = open(song_file, 'rb')
                            doc_input = InputFile(audio_fp, filename=os.path.basename(song_file))
                            await _retry_async(
                                context.bot.send_document,
                                chat_id=chat_id,
                                document=doc_input,
                                caption=(f"🎵 **{song_info['title']}**\n"
                                         f"🎤 Artist: {song_info['uploader']}\n"
                                         f"⏱️ Duration: {song_info['duration']}\n"
                                         f"📁 Size: {file_size_mb:.2f} MB\n"
                                         f"🎧 Quality: 128kbps MP3\n"
                                         f"👤 Requested by: {user_name}\n\n"
                                         "(Sent as file because streaming audio failed) 🎶"),
                                parse_mode='Markdown'
                            )
                            sent_ok = True
                        except Exception as e2:
                            logger.error(f"Fallback send_document failed: {e2}")
                            try:
                                await _retry_async(
                                    progress_msg.edit_text,
                                    f"❌ **Sending failed**\n\nError: {e2}\n\n"
                                    "Please try again or contact the bot admin.",
                                    parse_mode='Markdown'
                                )
                            except Exception:
                                pass

                    # Close file handles
                    try:
                        audio_fp.close()
                    except:
                        pass
                    if thumb_fp:
                        try:
                            thumb_fp.close()
                        except:
                            pass

                    # Cache the last sent audio for this user (for /cut)
                    try:
                        cache_dir = _ensure_cache_dir()
                        cache_name = _sanitize_filename(f"{song_info['title']}-{song_info['uploader']}") + '.mp3'
                        cached_path = os.path.join(cache_dir, cache_name)
                        # Copy original before cleanup
                        shutil.copy2(song_file, cached_path)
                        # Store reference in user_data
                        context.user_data['last_audio_path'] = cached_path
                        context.user_data['last_audio_title'] = song_info['title']
                        context.user_data['last_audio_artist'] = song_info['uploader']
                    except Exception as e:
                        logger.warning(f"Failed to cache last audio: {e}")

                    # Show success only if we actually sent something
                    if sent_ok:
                        try:
                            await _retry_async(
                                progress_msg.edit_text,
                                f"✅ **Success!** 🎉\n"
                                f"🎵 **{song_info['title']}** has been sent!\n\n"
                                "Want more music? Just send another song name! 🎶",
                                parse_mode='Markdown'
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await _retry_async(
                                progress_msg.edit_text,
                                "❌ **We couldn't send the track.**\n\n"
                                "Please try again, or pick another result.",
                                parse_mode='Markdown'
                            )
                        except Exception:
                            pass

                    # Clean up audio and thumbnail files
                    try:
                        if os.path.exists(song_file):
                            os.remove(song_file)
                    except:
                        pass
                    try:
                        if song_info.get('thumbnail') and os.path.exists(song_info['thumbnail']):
                            os.remove(song_info['thumbnail'])
                    except:
                        pass

                except Exception as e:
                    logger.error(f"Download error: {str(e)}")
                    await _retry_async(
                        progress_msg.edit_text,
                        f"❌ **Download failed** 😅\n\n"
                        f"**Error:** {str(e)}\n\n"
                        "Try selecting a different song or search again! 🔍",
                        parse_mode='Markdown'
                    )

                    # Clean up any files
                    import glob
                    for mp3_file in glob.glob("*.mp3"):
                        try:
                            os.remove(mp3_file)
                        except:
                            pass
                    # Clean up thumbnail files
                    for img_file in glob.glob("*.jpg") + glob.glob("*.jpeg") + glob.glob("*.png") + glob.glob("*.webp"):
                        try:
                            os.remove(img_file)
                        except:
                            pass
            else:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ Invalid selection. Please search again!",
                    parse_mode='Markdown'
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Invalid callback data. Please search again!",
                parse_mode='Markdown'
            )

    except Exception as e:
        logger.error(f"Callback error: {str(e)}")
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌ **Something went wrong!**\n\nPlease try searching again! 🔍",
            parse_mode='Markdown'
        )

async def handle_message(update, context):
    query = update.message.text.strip()
    
    # Handle empty messages
    if not query:
        await update.message.reply_text(
            "🤔 I didn't catch that!\n"
            "Send me a song name or use /help for assistance! 🎵"
        )
        return
    
    # Check for common greetings and respond accordingly
    greetings = ['hi', 'hello', 'hey', 'sup', 'yo', 'hola', 'namaste']
    if query.lower() in greetings:
        user_name = update.effective_user.first_name
        await update.message.reply_text(
            f"Hey {user_name}! 👋\n\n"
            "🎵 Ready to download some music?\n"
            "Just send me any song name!\n\n"
            "Use /help if you need assistance! 🎶"
        )
        return
    
    # Handle thanks
    thanks = ['thanks', 'thank you', 'thx', 'ty', 'awesome', 'great', 'cool']
    if any(word in query.lower() for word in thanks):
        await update.message.reply_text(
            "🤗 You're welcome!\n"
            "Happy to help you discover great music! 🎵\n\n"
            "Send me another song anytime! 🎶"
        )
        return
    
    # Process music request
    await handle_music_request(update, context, query)

async def cut_command(update, context):
    """Cut a segment from the last audio sent to this user using ffmpeg.
    Usage: /cut <start> <end> where timestamps support ss, mm:ss, hh:mm:ss, or 1m30s.
    """
    # Ensure cache cleanup occasionally
    _cleanup_cache()

    # Validate args
    if len(context.args) != 2:
        await update.message.reply_text(
            "✂️ Usage: `/cut <start> <end>`\n\n"
            "Examples:\n"
            "• `/cut 30 75` (00:30 → 01:15)\n"
            "• `/cut 1:05 2:10` (mm:ss)\n"
            "• `/cut 1m 1m30s`",
            parse_mode='Markdown'
        )
        return

    try:
        start_sec = _parse_timestamp_to_seconds(context.args[0])
        end_sec = _parse_timestamp_to_seconds(context.args[1])
        if end_sec <= start_sec:
            raise ValueError("End must be greater than start")
    except Exception as e:
        await update.message.reply_text(
            f"❌ Invalid timestamps: {e}\n"
            "Use formats like `75`, `1:15`, `01:15`, `1m15s`, or `0:45 3:00`.",
            parse_mode='Markdown'
        )
        return

    # Retrieve last audio path from user_data
    last_audio = context.user_data.get('last_audio_path')
    title = context.user_data.get('last_audio_title', 'audio')
    artist = context.user_data.get('last_audio_artist', '')
    if not last_audio or not os.path.exists(last_audio):
        await update.message.reply_text(
            "😕 I couldn't find your last audio file.\n"
            "Please download a song first, then run `/cut <start> <end>`.",
            parse_mode='Markdown'
        )
        return

    # Prepare output path
    cache_dir = _ensure_cache_dir()
    base_name = _sanitize_filename(f"{title}-cut-{start_sec}-{end_sec}") + '.mp3'
    output_path = os.path.join(cache_dir, base_name)

    # Progress message
    progress = await update.message.reply_text(
        f"✂️ Cutting `[{context.args[0]} → {context.args[1]}]` from:\n"
        f"• {title}\n\n"
        "This may take a few seconds...",
        parse_mode='Markdown'
    )

    # Perform cut
    try:
        await _ffmpeg_cut_audio(last_audio, start_sec, end_sec, output_path)
    except Exception as e:
        logger.error(f"ffmpeg cut error: {e}")
        await progress.edit_text(
            f"❌ Failed to cut audio: {e}",
            parse_mode='Markdown'
        )
        return

    # Validate size
    try:
        size = os.path.getsize(output_path)
        if size > 50 * 1024 * 1024:
            os.remove(output_path)
            await progress.edit_text(
                "❌ Clipped file exceeds 50MB. Try a shorter segment.",
                parse_mode='Markdown'
            )
            return
    except Exception:
        pass

    # Send the clipped audio
    try:
        await _retry_async(context.bot.send_chat_action, chat_id=update.effective_chat.id, action=ChatAction.UPLOAD_AUDIO)
    except Exception:
        pass

    try:
        with open(output_path, 'rb') as f:
            audio_input = InputFile(f, filename=os.path.basename(output_path))
            await _retry_async(
                context.bot.send_audio,
                chat_id=update.effective_chat.id,
                audio=audio_input,
                title=f"{title} (cut)",
                performer=artist or None,
                caption=(f"✂️ `{context.args[0]} → {context.args[1]}`\n"
                         f"🎵 {title}"),
                parse_mode='Markdown'
            )
        await progress.edit_text("✅ Sent the clipped audio!", parse_mode='Markdown')
    except Exception as e:
        logger.error(f"send cut audio failed: {e}")
        try:
            await progress.edit_text(
                f"❌ Failed to send clipped audio: {e}",
                parse_mode='Markdown'
            )
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(output_path):
                os.remove(output_path)
        except Exception:
            pass

def main():
    logger.info("Starting bot...")
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN is not set. Please set environment variable BOT_TOKEN or provide it in a .env file.")
        raise SystemExit(1)
    
    # Build application with better timeout settings
    async def _post_init(app):
        try:
            commands = [
                BotCommand("start", "Show welcome message"),
                BotCommand("help", "How to use the bot"),
                BotCommand("search", "Search and download a song"),
                BotCommand("cut", "Cut a part of the last audio"),
                BotCommand("stats", "Bot statistics"),
                BotCommand("about", "About this bot"),
            ]
            await app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
            await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
            await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
        except Exception as e:
            logger.warning(f"Failed to set bot commands: {e}")

    application = (Application.builder()
                  .token(BOT_TOKEN)
                  .read_timeout(300)    # 5 minutes read timeout
                  .write_timeout(300)   # 5 minutes write timeout
                  .connect_timeout(60)  # 1 minute connection timeout
                  .post_init(_post_init)
                  .build())

    # Add command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("cut", cut_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("about", about_command))
    
    # Add callback handler for download buttons
    application.add_handler(CallbackQueryHandler(handle_download_callback))
    
    # Add message handler for song requests and conversations
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bot is now running and polling for messages...")
    
    # Run with better polling settings
    application.run_polling(
        timeout=30,           # Poll every 30 seconds
        drop_pending_updates=True  # Clear any pending updates on startup
    )

if __name__ == '__main__':
    main()
