import asyncio
import os
import re
import random
import sys
from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import GetFullChatRequest
from telethon.errors import FloodWaitError

TRACK_FILE = 'joined_channels_groups.txt'

# adaugă căile relative la directorul scriptului (blacklist în același folder cu acest script)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_IMAGE = os.path.join(SCRIPT_DIR, 'Media', 'creo', 'creative.jpg')
MEDIA_TEXT_FILE = os.path.join(SCRIPT_DIR, 'Media', 'text_message', 'message.txt')
BLACKLIST_FILE = os.path.join(SCRIPT_DIR, 'blacklist.txt')
ACTIVE_FILE = os.path.join(SCRIPT_DIR, 'active_chats.txt')

async def search_public_groups(client, query, limit, fetch_limit=100):
    found = []
    seen_usernames = set()
    # cerem rezultate mai multe pentru a avea de unde filtra (minim fetch_limit)
    resp = await client(SearchRequest(q=query, limit=max(fetch_limit, limit * 5)))
    chats = getattr(resp, 'chats', []) or []
    for c in chats:
        username = getattr(c, 'username', None)
        if not username:
            continue  # vrem doar entități publice cu username -> link t.me
        is_channel = isinstance(c, Channel)
        is_chat = isinstance(c, Chat)
        if is_channel:
            if getattr(c, 'broadcast', False) and not getattr(c, 'megagroup', False):
                continue
        link = f"https://t.me/{username}"
        if username not in seen_usernames:
            seen_usernames.add(username)
            found.append((getattr(c, 'title', username), link))
        if len(found) >= limit:
            break
    return found

def sanitize_filename(s):
    s = s.strip().lower()
    s = re.sub(r'\s+', '_', s)
    s = re.sub(r'[^a-z0-9_\-]', '', s)
    return s or 'search'

def load_countries(path):
    if not os.path.exists(path):
        # dacă nu există, creează cu exemple
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

async def get_member_count_best_effort(client, username):
    # username poate fi 't.me/name' sau 'name'
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
        # fallback: multe entități nu permit sau pot arunca erori
        return None
    return None

async def try_send_with_join(client, entity_ident, caption, max_retries=3):
    """
    Încearcă până la max_retries să trimită media+caption la entity_ident.
    Aplică backoff progresiv la FloodWait și încearcă join dacă este necesar.
    Returnează True dacă s-a livrat, False altfel.
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
            wait = e.seconds * attempt  # backoff progresiv
            print(f"FloodWait (attempt {attempt}/{max_retries}): aștept {wait}s...")
            await asyncio.sleep(wait + 1)
            # retry loop continues
        except Exception as e:
            # dacă nu putem trimite: încercăm join o singură dată (dacă nu s-a făcut)
            if not joined:
                try:
                    await client(JoinChannelRequest(entity))
                    joined = True
                    await asyncio.sleep(1.5)
                    # retry imediat după join
                except FloodWaitError as fe:
                    wait = fe.seconds * attempt
                    print(f"FloodWait la join (attempt {attempt}/{max_retries}): aștept {wait}s...")
                    await asyncio.sleep(wait + 1)
                except Exception:
                    # nu se poate join; se mai încercă alte tentative (dacă există)
                    pass
            else:
                # altă eroare după join sau retry nereușit
                await asyncio.sleep(1 * attempt)  # mic backoff înainte de retry
    return False

async def wait_for_exit():
    """
    Async non-blocant: așteaptă ESC pe Windows sau Enter pe alte platforme.
    """
    if sys.platform.startswith('win'):
        try:
            import msvcrt
        except Exception:
            # fallback la blocking input dacă nu este disponibil
            await asyncio.to_thread(input, "Apăsați Enter pentru a închide...")
            return
        print("Aștept ESC (apăsați ESC pentru a închide)...")
        while True:
            await asyncio.sleep(0.1)
            if msvcrt.kbhit():
                ch = msvcrt.getch()
                if ch == b'\x1b':  # ESC
                    return
    else:
        # non-blocking wrapper pentru input în thread
        await asyncio.to_thread(input, "Apăsați Enter pentru a închide...")

async def main():
    api_id = 25857306
    api_hash = '4f8f637d699905e4b6e18f6dbf590533'
    session_name = 'simple_session'
    client = TelegramClient(session_name, api_id, api_hash)
    print('Conectare la Telegram...')
    async with client:
        await client.start()
        me = await client.get_me()
        print(f'Conectat: {me.first_name} ({me.id})')

        # încarcă țările din fișier
        countries_file = os.path.join(os.getcwd(), 'countrys.txt')
        countries = load_countries(countries_file)
        if not countries:
            print(f"Fișierul {countries_file} este gol; adăugați țări (una pe linie) și rulați din nou.")
            return
        print(f"Țări încărcate: {len(countries)}")

        # Interacțiune simplă cu utilizatorul pentru keywords și limită per combinație
        queries_raw = input("Introduceți una sau mai multe chei de căutare (separate prin virgule), ex: chat, sales, cars: ").strip()
        if not queries_raw:
            print("Nicio cheie introdusă. Ieșire.")
            return
        keywords = [q.strip() for q in queries_raw.split(',') if q.strip()]
        try:
            per_combo_limit = int(input("Număr maxim de grupuri per combinație (ex: 100): ").strip() or "100")
        except ValueError:
            per_combo_limit = 100

        # fetch_limit mărit la minim 100 pentru a obține mai multe rezultate din API
        fetch_limit = 100

        output_dir = os.path.join(os.getcwd(), 'search_results')
        os.makedirs(output_dir, exist_ok=True)

        total_unique_links = {}
        per_country_counts = {}

        for country in countries:
            found_links = {}
            print(f"\nProcesare țară: '{country}' cu {len(keywords)} keywords...")
            # generează combinații inteligente: "country keyword", "keyword country" și "country"
            combos = set()
            combos.add(country)
            for kw in keywords:
                combos.add(f"{country} {kw}")
                combos.add(f"{kw} {country}")
            for combo in combos:
                print(f"  Căutare: '{combo}' ...")
                results = await search_public_groups(client, combo, per_combo_limit, fetch_limit=fetch_limit)
                for title, link in results:
                    found_links[link] = title
            safe = sanitize_filename(country)
            out_path = os.path.join(output_dir, f"{safe}.txt")
            with open(out_path, 'w', encoding='utf-8') as f:
                for link in found_links.keys():
                    f.write(link + '\n')
            per_country_counts[country] = {
                'file': out_path,
                'count': len(found_links)
            }
            # agregare globală (dedup)
            for link, title in found_links.items():
                total_unique_links[link] = title
            print(f"  Găsite unice pentru '{country}': {len(found_links)} -> {out_path}")

        # Statistici finale
        print("\n=== Statistici de căutare ===")
        total_links = len(total_unique_links)
        print(f"Total linkuri unice găsite: {total_links}")
        for country, info in per_country_counts.items():
            print(f" - {country}: {info['count']} (fișier: {info['file']})")

        # încercă să obții numărul de conturi (membri) pentru fiecare link - best-effort
        member_counts = {}
        if total_links == 0:
            print("Nu s-au găsit linkuri, niciun număr de conturi de raportat.")
        else:
            # limitează numărul de cereri la API pentru a evita blocarea; dacă prea multe, sari
            if total_links > 200:
                print(f"Au fost găsite {total_links} linkuri — prea multe pentru a interoga member counts (limitat la 200). Sari această etapă.")
            else:
                print("Se încearcă obținerea numărului de membri pentru fiecare link (best-effort)...")
                for link in total_unique_links.keys():
                    uname = link.rsplit('/', 1)[-1]
                    try:
                        cnt = await get_member_count_best_effort(client, uname)
                        member_counts[link] = cnt
                        print(f"  {link} -> members: {cnt}")
                    except Exception:
                        member_counts[link] = None

                # sumare
                known_counts = [c for c in member_counts.values() if isinstance(c, int)]
                if known_counts:
                    print(f"\nStatistici membri (doar pentru cele obținute): min={min(known_counts)}, max={max(known_counts)}, total_sum={sum(known_counts)}, avg={sum(known_counts)/len(known_counts):.1f}")
                else:
                    print("Nu s-a putut obține niciun member count.")

        # Pregătire pentru trimitere mesaje
        blacklist = load_lines(BLACKLIST_FILE)
        active_chats = load_lines(ACTIVE_FILE)
        caption = read_message_text(MEDIA_TEXT_FILE)
        if not caption:
            # fallback la textul furnizat în prompt
            caption = ("🤩 CLAIM 100 FREE SPINS ON SIGN-UP!\n\n"
                       "🎁 100 FREE SPINS – Just for creating your account!\n"
                       "🔥 500% BONUS – We multiply your first deposit!\n"
                       "🎰 +70 EXTRA SPINS – Added to your welcome package.\n\n"
                       "Use code FLYWINCASH to unlock all bonuses! 💰\n\n"
                       "➡️ CLAIM BONUS NOW (https://bit.ly/4qAOls7)\n" * 1)

        # Parametru user: câte mesaje livrate dorim în total
        try:
            delivered_target = int(input("\nCate mesaje doriti livrate in total (ex: 5): ").strip() or "5")
        except Exception:
            delivered_target = 5

        # retries per chat (user-visible, set implicit)
        try:
            per_chat_retries = int(input("Număr maxim încercări per chat (ex: 3): ").strip() or "3")
        except Exception:
            per_chat_retries = 3

        delivered = 0
        links_list = list(total_unique_links.keys())

        print(f"\nStart sending. Target delivered messages: {delivered_target}")
        # Ciclăm până atingem target sau nu mai avem linkuri disponibile
        tried = set()
        round_num = 0
        while delivered < delivered_target:
            round_num += 1
            made_progress = False
            print(f"\nRunda {round_num}: delivered={delivered}, linkuri disponibile={len(links_list)}")
            for link in links_list:
                if delivered >= delivered_target:
                    break
                if link in blacklist:
                    continue
                if link in tried and round_num > 1:
                    continue
                print(f"Încerc transmitere către: {link} ...")
                # încearcă cu retry limit per chat
                success = await try_send_with_join(client, link, caption, max_retries=per_chat_retries)
                tried.add(link)
                # delay random între încercări pentru a părea natural
                await asyncio.sleep(random.uniform(1.0, 3.0))
                if success:
                    print(f"  Delivered to {link}")
                    delivered += 1
                    made_progress = True
                    if link not in active_chats:
                        append_line(ACTIVE_FILE, link)
                        active_chats.add(link)
                else:
                    print(f"  FAILED -> adăugat în blacklist: {link}")
                    append_line(BLACKLIST_FILE, link)
                    blacklist.add(link)
            if not made_progress:
                # nu s-a livrat nimic în această rundă -> nu putem continua
                print("Nu s-a reușit nicio livrare în această rundă. Oprire pentru a evita loop infinit.")
                break

        print(f"\nFinal sending: delivered={delivered} / target={delivered_target}")
        print("Lista active chats (schiță):", len(active_chats))
        print("Lista blacklist:", len(blacklist))

        # așteaptă async non-blocant exit
        await wait_for_exit()
        print("Ieșire finală.")

if __name__ == '__main__':
    asyncio.run(main())
