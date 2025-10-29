import asyncio
import json
import os
import re
import random
import sys
import time
import logging
from enum import Enum
from typing import Optional

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.errors import FloodWaitError

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_IMAGE = os.path.join(SCRIPT_DIR, 'Media', 'creo', 'creative.jpg')
MEDIA_TEXT_FILE = os.path.join(SCRIPT_DIR, 'Media', 'text_message', 'message.txt')
BLACKLIST_FILE = os.path.join(SCRIPT_DIR, 'blacklist.txt')
ACTIVE_FILE = os.path.join(SCRIPT_DIR, 'active_chats.txt')
STATE_FILE = os.path.join(SCRIPT_DIR, 'state.json')
CONFIG_FILE = os.path.join(SCRIPT_DIR, 'config.json')

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s: %(message)s')
logger = logging.getLogger('tg-autoposter')


class State(str, Enum):
    SEARCH = 'SEARCH'
    SEND = 'SEND'
    SLEEP = 'SLEEP'
    PAUSE = 'PAUSE'


def sanitize_filename(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[^a-z0-9_\-]', '', s)
    return s or 'search'


def load_countries(path):
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write("Mexico\nUSA\nSpain\n")
    with open(path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip()]


def load_lines(path):
    if not os.path.exists(path):
        return set()
    with open(path, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())


def append_line(path, line):
    with open(path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def read_message_text(path):
    if not os.path.exists(path):
        return ""
    with open(path, 'r', encoding='utf-8') as f:
        return f.read().strip()


async def search_public_groups(client, query, limit, fetch_limit=100):
    found = []
    seen_usernames = set()
    try:
        resp = await client(SearchRequest(q=query, limit=max(fetch_limit, limit * 5)))
    except Exception as e:
        logger.debug('Search request failed: %s', e)
        return []
    chats = getattr(resp, 'chats', []) or []
    for c in chats:
        username = getattr(c, 'username', None)
        if not username:
            continue
        is_channel = isinstance(c, Channel)
        if is_channel and getattr(c, 'broadcast', False) and not getattr(c, 'megagroup', False):
            # skip pure broadcast channels
            continue
        link = f"https://t.me/{username}"
        if username not in seen_usernames:
            seen_usernames.add(username)
            found.append((getattr(c, 'title', username), link))
        if len(found) >= limit:
            break
    return found


async def get_member_count_best_effort(client, username):
    uname = username.rsplit('/', 1)[-1]
    try:
        entity = await client.get_entity(uname)
    except Exception:
        return None
    try:
        if isinstance(entity, Channel):
            full = await client(GetFullChannelRequest(entity))
            return getattr(full.full_chat, 'participants_count', None)
        elif isinstance(entity, Chat):
            full = await client(GetFullChatRequest(entity.id))
            return getattr(full.full_chat, 'participants_count', None)
    except Exception:
        return None
    return None


async def try_send_with_join(client, entity_ident, caption, config, max_retries=3):
    """
    Try to send file+caption with backoff and optional join. Returns True if delivered.
    """
    uname = entity_ident.rsplit('/', 1)[-1]
    attempt = 0
    joined = False
    while attempt < max_retries:
        attempt += 1
        try:
            entity = await client.get_entity(uname)
        except Exception:
            return False
        try:
            await client.send_file(entity, MEDIA_IMAGE, caption=caption)
            return True
        except FloodWaitError as e:
            wait = int(e.seconds * (1 + attempt * config.get('flood_wait_multiplier', 1)))
            logger.warning('FloodWait: waiting %ds (attempt %d/%d)', wait, attempt, max_retries)
            await asyncio.sleep(wait + random.uniform(0.5, 2.0))
        except Exception as e:
            logger.debug('send exception: %s', e)
            if not joined:
                try:
                    await client(JoinChannelRequest(entity))
                    joined = True
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                except FloodWaitError as fe:
                    wait = int(fe.seconds * (1 + attempt))
                    logger.warning('FloodWait on join: waiting %ds', wait)
                    await asyncio.sleep(wait + 1)
                except Exception:
                    # cannot join; fail this attempt and continue
                    await asyncio.sleep(random.uniform(0.5, 1.5))
            else:
                await asyncio.sleep(random.uniform(0.5, 1.5) * attempt)
    return False


class TaskManager:
    """Manages search+send cycles with persistence and safety heuristics."""

    def __init__(self, client: TelegramClient, config: dict):
        self.client = client
        self.config = config
        self.state = State.SEARCH
        self.state_data = {}
        self.blacklist = load_lines(BLACKLIST_FILE)
        self.active_chats = load_lines(ACTIVE_FILE)
        self.countries = load_countries(os.path.join(os.getcwd(), 'countrys.txt'))
        self.keywords = []
        self.load_state()

    def load_state(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.state = State(data.get('state', State.SEARCH))
                    self.state_data = data.get('state_data', {})
                    self.delivered = data.get('delivered', 0)
            except Exception:
                self.state = State.SEARCH
                self.state_data = {}
                self.delivered = 0
        else:
            self.state = State.SEARCH
            self.state_data = {}
            self.delivered = 0

    def save_state(self):
        data = {
            'state': self.state.value,
            'state_data': self.state_data,
            'delivered': getattr(self, 'delivered', 0),
            'timestamp': int(time.time())
        }
        try:
            with open(STATE_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f)
        except Exception as e:
            logger.debug('Could not save state: %s', e)

    def build_combos(self):
        combos = []
        for country in self.countries:
            combos.append(country)
            for kw in self.keywords:
                combos.append(f"{country} {kw}")
                combos.append(f"{kw} {country}")
        # unique preserve order
        seen = set()
        out = []
        for c in combos:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out

    async def run(self):
        combos = self.build_combos()
        combo_index = self.state_data.get('combo_index', 0)
        caption = read_message_text(MEDIA_TEXT_FILE) or self.config.get('default_caption', '')
        max_messages = self.config.get('delivered_target', 10)
        per_combo_limit = self.config.get('per_combo_limit', 50)

        while getattr(self, 'delivered', 0) < max_messages:
            try:
                if combo_index >= len(combos):
                    logger.info('All combos processed; sleeping for %ds', self.config.get('cycle_sleep_seconds', 3600))
                    await asyncio.sleep(self.config.get('cycle_sleep_seconds', 3600))
                    combo_index = 0
                    continue

                combo = combos[combo_index]
                logger.info('Searching combo [%d/%d]: "%s"', combo_index + 1, len(combos), combo)
                results = await search_public_groups(self.client, combo, per_combo_limit, fetch_limit=self.config.get('fetch_limit', 100))
                # Mix: for each found channel attempt send immediately if allowed
                for title, link in results:
                    if getattr(self, 'delivered', 0) >= max_messages:
                        break
                    if link in self.blacklist:
                        continue
                    if link in self.active_chats:
                        logger.debug('Already active: %s', link)
                        continue
                    # quick check: optional member count threshold
                    min_members = self.config.get('min_members', 0)
                    if min_members > 0:
                        cnt = await get_member_count_best_effort(self.client, link)
                        if isinstance(cnt, int) and cnt < min_members:
                            logger.debug('Skip %s: too small (%s members)', link, cnt)
                            continue
                    logger.info('Attempting send to %s (%s)', title, link)
                    success = await try_send_with_join(self.client, link, caption, self.config, max_retries=self.config.get('per_chat_retries', 3))
                    # random small delay after each attempt to mimic human behaviour
                    await asyncio.sleep(random.uniform(self.config.get('min_delay', 1.0), self.config.get('max_delay', 3.0)))
                    if success:
                        self.delivered = getattr(self, 'delivered', 0) + 1
                        append_line(ACTIVE_FILE, link)
                        self.active_chats.add(link)
                        logger.info('Delivered (%d/%d) -> %s', self.delivered, max_messages, link)
                    else:
                        append_line(BLACKLIST_FILE, link)
                        self.blacklist.add(link)
                        logger.warning('Failed -> blacklisted: %s', link)
                    # intermediate persist
                    self.state_data['combo_index'] = combo_index
                    self.save_state()

                combo_index += 1
                self.state_data['combo_index'] = combo_index
                self.save_state()

                # adaptive rest between combos
                chunk_sleep = random.uniform(self.config.get('combo_delay_min', 2.0), self.config.get('combo_delay_max', 6.0))
                await asyncio.sleep(chunk_sleep)

            except FloodWaitError as fe:
                wait = fe.seconds if hasattr(fe, 'seconds') else self.config.get('flood_wait_default', 60)
                logger.warning('Global FloodWait caught, sleeping %ds', wait)
                await asyncio.sleep(wait + 5)
            except Exception as e:
                logger.exception('Unexpected error in run loop: %s', e)
                # avoid tight crash loop
                await asyncio.sleep(10)

        logger.info('Target reached: delivered=%d', getattr(self, 'delivered', 0))
        self.save_state()


async def wait_for_exit():
    if sys.platform.startswith('win'):
        try:
            import msvcrt
        except Exception:
            await asyncio.to_thread(input, "Press Enter to exit...")
            return
        logger.info('Waiting for ESC to exit...')
        while True:
            await asyncio.sleep(0.1)
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b':
                    return
    else:
        await asyncio.to_thread(input, "Press Enter to exit...")


def load_config():
    default = {
        'per_combo_limit': 30,
        'fetch_limit': 100,
        'delivered_target': 5,
        'per_chat_retries': 3,
        'min_delay': 1.0,
        'max_delay': 3.0,
        'combo_delay_min': 2.0,
        'combo_delay_max': 6.0,
        'cycle_sleep_seconds': 3600,
        'min_members': 0,
        'flood_wait_multiplier': 1.0,
        'flood_wait_default': 60,
        'default_caption': '',
    }
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                cfg = json.load(f)
                default.update(cfg)
        except Exception:
            logger.warning('Could not read config.json, using defaults')
    else:
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(default, f, indent=2)
                logger.info('Created default config.json — tweak it and re-run as needed')
        except Exception:
            logger.debug('Could not write default config file')
    return default


async def main():
    # Put your API keys here or use environment variables
    api_id = int(os.environ.get('TG_API_ID', '25857306'))
    api_hash = os.environ.get('TG_API_HASH', '4f8f637d699905e4b6e18f6dbf590533')
    session_name = os.environ.get('TG_SESSION', 'simple_session')

    config = load_config()
    # allow runtime overrides
    try:
        delivered_target = int(input(f"Messages to deliver (default {config.get('delivered_target')}): ").strip() or config.get('delivered_target'))
        config['delivered_target'] = delivered_target
    except Exception:
        pass
    queries_raw = input('Keywords (comma separated, e.g. cars,crypto): ').strip()
    keywords = [q.strip() for q in queries_raw.split(',') if q.strip()]

    client = TelegramClient(session_name, api_id, api_hash)
    async with client:
        await client.start()
        me = await client.get_me()
        logger.info('Connected: %s (%s)', getattr(me, 'first_name', 'me'), me.id)

        manager = TaskManager(client, config)
        manager.keywords = keywords

        # load custom caption from file if available
        caption = read_message_text(MEDIA_TEXT_FILE)
        if caption:
            config['default_caption'] = caption

        # Supervisor loop to keep process alive and restart manager.run on unexpected stops
        while True:
            try:
                await manager.run()
                break
            except Exception as e:
                logger.exception('Manager crashed: %s — restarting after backoff', e)
                await asyncio.sleep(10)

        logger.info('Worker finished or target reached. Waiting for exit...')
        await wait_for_exit()


if __name__ == '__main__':
    asyncio.run(main())
