#!/usr/bin/env python3
"""KORS Tire — call card bot.

One card = one lead in Kommo. The rep fills a paper card during the call and
photographs it. This bot reads the card, SHOWS WHAT IT READ, waits for the rep
to confirm, and only then writes to Kommo.

Three rules this file exists to enforce:

  1. Nothing is written to Kommo until a human has confirmed the reading.
     Handwriting is the only thing that can go wrong here, and the only person
     who can check it is the one who wrote it.

  2. Nothing is guessed. Every field is either read off the card or left empty.
     The old bot inferred tire type from substrings in the brand name and took
     the store from the colour of the paper. Both are gone.

  3. Every lead gets a next step. A lead with no task is a lost lead, and that
     is the whole reason this project exists.

Replaces the sticker-reading bot of 5 August 2026, which nobody used.
"""
import os
import io
import re
import json
import html
import base64
import logging
import datetime

import requests
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, MessageHandler, CommandHandler,
                          CallbackQueryHandler, filters, ContextTypes)

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger("korscard")

# ---------------------------------------------------------------- environment

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
KOMMO_TOKEN = os.environ['KOMMO_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
ADMIN_CHAT_ID = os.environ.get('ADMIN_CHAT_ID')

KOMMO_SUBDOMAIN = os.environ.get('KOMMO_SUBDOMAIN', 'korstire')
KOMMO_BASE = f'https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4'

# The call log. A card is written during a phone call, so the number on it
# should appear in the log of calls. When it does not, the digits are usually
# wrong - which is exactly what happened on the first live card.
AIRTABLE_TOKEN = os.environ.get('AIRTABLE_TOKEN')
AIRTABLE_BASE = os.environ.get('AIRTABLE_BASE', 'appMXQet1q0HaBvZo')
AIRTABLE_CALLS = os.environ.get('AIRTABLE_CALLS', 'tblUkCo2Y47f5viwx')
CALL_PHONE_FIELD = 'fldfgDIRXEmwYbzne'
CALL_DATE_FIELD = 'fld7Gzj68GGx0Txpc'
CALL_LOOKBACK_DAYS = int(os.environ.get('CALL_LOOKBACK_DAYS', '3'))

# The container runs on UTC. Tasks are for people in Kelowna and Vernon, so
# every due date is worked out in their time. 31 August: the first live card
# produced a follow-up task due at 03:00, because "tomorrow at 10:00" was
# 10:00 UTC.
try:
    from zoneinfo import ZoneInfo
    LOCAL_TZ = ZoneInfo(os.environ.get('LOCAL_TZ', 'America/Vancouver'))
except Exception as _tz_exc:          # tzdata missing - better wrong than dead
    logging.error("Timezone database unavailable (%s), falling back to -07:00", _tz_exc)
    LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=-7))

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
CLAUDE_MODEL = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-5')

# Who sent the photo decides which store the lead belongs to. This is a fact
# (we know the Telegram account), unlike the colour of the paper, which the old
# bot used to guess it. Format: "123456789:Kelowna,987654321:Vernon".
STORE_BY_USER = {}
for pair in os.environ.get('STORE_BY_USER', '').split(','):
    if ':' in pair:
        uid, store = pair.split(':', 1)
        STORE_BY_USER[uid.strip()] = store.strip()

# The scan itself. Kommo holds what the bot READ; this folder holds what the rep
# actually WROTE. When the two disagree, the paper is the truth, and the lead
# manager needs to be able to look at it without asking anyone for the card.
DROPBOX_APP_KEY = os.environ.get('DROPBOX_APP_KEY')
DROPBOX_APP_SECRET = os.environ.get('DROPBOX_APP_SECRET')
DROPBOX_REFRESH_TOKEN = os.environ.get('DROPBOX_REFRESH_TOKEN')

# ------------------------------------------------------------- Kommo constants
# All of these were read off GET /api/v4/*/custom_fields on 29 August 2026.
# None of them are remembered or assumed.

PIPELINE_ID = 11129295

STAGE_NEW = 85390759          # New Lead
STAGE_IN_CONTACT = 110074519  # In Contact
STAGE_QUOTE_SENT = 85390763   # Quote Sent
STAGE_BOOKED = 85390771       # Booked for Install
CLOSED_WON = 142
CLOSED_LOST = 143

F_SIZE = 983549               # Size, text
F_TIRE_BRAND = 983835         # multiselect
F_SOURCE = 983837             # multiselect
F_LOCATION = 983839           # select
F_INSTALLATION = 983843       # multiselect
F_RESULT = 974690             # multiselect
F_WAITING_FOR = 997558        # select
F_CONTACT_PHONE = 807760      # multitext
F_CONTACT_VEHICLE = 997380    # text
F_CONTACT_GOES_BY = 997378    # text

E_BRAND_LOCAL = 836283
E_INSTALL_YES = 836315
E_RESULT_OUT_OF_STOCK = 822210
E_WAITING_OUR_STOCK = 919174

LOCATION_ENUM = {"kelowna": 836301, "vernon": 836303,
                 "vancouver island": 836305, "online": 836307}

# HEARD OF US on the card -> Source in Kommo. Five options, five enums, no
# fuzzy matching: the card has tick boxes, so the answer is one of these or
# nothing at all.
SOURCE_ENUM = {"google": 836291,
               "castanet": 836293,
               "facebook": 836287,
               "referral": 836289,
               "repeat": 836285}

HEADERS = {'Authorization': f'Bearer {KOMMO_TOKEN}',
           'Content-Type': 'application/json'}

# ------------------------------------------------------------------ the prompt

CARD_PROMPT = """You are reading a photograph of a KORS Tire paper call card.
The card is a printed form. The rep fills it in by hand during a phone call.

Report ONLY what is physically written or ticked on the card. If a field is
blank, return null. Never infer a value from another field. Never complete a
partial number. If you cannot read something, return null rather than a guess.

Two rules that come from a real misreading on 29 August 2026:

  A. The card carries faint grey PRINTED examples next to several fields:
     "250 869 7186" under the phone boxes, "275/60R20" and "35X12.50R20"
     beside the size rows, "whole dollars, no cents" under the price boxes.
     These are printed on every blank card. They are NEVER the customer's
     data. Read only the darker handwriting inside the boxes. On the first
     live card the phone came back as 250 651-1465 when the boxes plainly
     held 250 571-4654.

  B. A tick box counts as ticked ONLY if there is a visible pen stroke inside
     the square. An empty printed square is not a tick. On the same card
     "with installation" was reported as ticked when both squares were bare.
     When in doubt, report false and add the field to "unreadable".

The printed layout, so you know where to look:

- PHONE NUMBER: exactly ten boxes, grouped 3-3-4, one digit per box. Read them
  one box at a time, left to right, and do not reorder or drop any. If a box
  is empty, the number is short - say so in "unreadable" rather than inventing
  a digit.
- NAME: one handwritten line - the person.
- COMPANY: one handwritten line, often blank. Only filled when the customer
  is buying for a business. Never put a person's name here and never put a
  company in NAME - the card keeps them apart on purpose.
- VEHICLE: one handwritten line, meant to hold year, make and model.
- TIRE SIZE: two rows of boxes. The separators / X . R are pre-printed on the
  card, the rep only fills digits. Row one is like 275/60R20, row two is like
  35X12.50R20. Only one row is used.
- STOCK: three tick boxes - "in stock", "out of stock", "local offer". More
  than one can be ticked.
- PRICE QUOTED (ALL IN, taxes in): two lines, OURS and LOCAL. Each has five
  boxes for whole dollars, filled from the RIGHT, and a tick box "with
  installation". A leading empty box means the number is shorter, not that a
  digit is missing: boxes reading _ 1 7 7 6 are 1776, not 17760.
- BOOKED: one tick box. Ticked means the customer is booked for installation.
- HEARD OF US: five tick boxes - Google, Castanet, Facebook, referral,
  been here before.

Answer in two steps, both inside the JSON.

STEP 1 - evidence. Before deciding anything, write down what you actually see:

  "phone_boxes": one entry per box, left to right, ten of them. Each entry is
    the single digit written in that box, or null if the box is empty. Do not
    write the number as a whole - go box by box, and count the boxes as you go.

  "ticked": the list of tick boxes that have ink in them. Use these names and
    no others: in_stock, out_of_stock, local_offer, ours_with_installation,
    local_with_installation, booked, google, castanet, facebook, referral,
    been_here_before. A box goes in this list ONLY if you can see a pen stroke
    inside the square. If the list is empty, return []. Do not add a box
    because the card would "make sense" with it ticked.

STEP 2 - the rest.

Return ONLY valid JSON, no markdown fence, no commentary:

{
  "phone_boxes": ["2","5","0","5","7","1","4","6","5","4"],
  "ticked": ["out_of_stock", "local_offer", "booked", "facebook"],
  "name": "the person, as written, or null",
  "company": "the business, as written, or null",
  "vehicle": "as written, or null",
  "size": "as written with its separators, e.g. 275/60R20, or null",
  "price_ours": whole number or null,
  "price_local": whole number or null,
  "unreadable": ["fields you could not READ. A blank box is not\n                  unreadable - a blank box is simply an answer of no."]
}

The two examples above are the shape, not the answer. Read the card in front
of you."""

TEXT_PROMPT = """The following is a correction typed by a KORS Tire rep for a
call card that was just read from a photograph. Return ONLY the fields the rep
is correcting, as valid JSON using these exact keys where they apply: phone,
name, company, vehicle, size, stock_in, stock_out, stock_local, price_ours,
price_ours_install, price_local, price_local_install, booked, heard.

Return an empty object {} if nothing in the message is a field correction.
Do not invent fields the rep did not mention.

Rep's message:
"""


TICK_NAMES = ("in_stock", "out_of_stock", "local_offer",
              "ours_with_installation", "local_with_installation", "booked",
              "google", "castanet", "facebook", "referral", "been_here_before")

HEARD_FROM_TICK = {"google": "google", "castanet": "castanet",
                   "facebook": "facebook", "referral": "referral",
                   "been_here_before": "repeat"}


SIZE_SHAPES = (re.compile(r'^\d{3}/\d{2}R\d{2}$'),
               re.compile(r'^\d{2}X\d{2}\.\d{2}R\d{2}$'))


def clean_size(value):
    """Keep the size only if it has a shape the card can physically hold.

    The two rows are [3][3? no - 2] boxes with printed separators, so the only
    possible answers are 305/55R20 and 35X12.50R20. 31 August a card came back
    as "305/155R20": the machine read the printed "/" as a handwritten 1. Three
    digits do not fit in that group, so the answer was impossible on its face -
    and a plausible wrong size is worse than an empty field, because nobody
    catches it.
    """
    v = (value or '').strip().upper().replace(' ', '')
    if not v:
        return None, False
    if any(p.match(v) for p in SIZE_SHAPES):
        return v, False
    log.info("Size %r does not fit either row of the card - dropped", value)
    return None, True


def normalise_card(raw: dict) -> dict:
    """Turn the model's evidence into the flat card the rest of the code uses.

    The model is asked for what it SEES - a digit per box, a list of boxes with
    ink in them - and the booleans are worked out here. 29 August: asked for
    booleans directly, it twice reported "with installation" as ticked on a
    card where both squares were bare. Listing what has ink is a smaller thing
    to get wrong than filling in eleven true/false answers.
    """
    ticked = {t for t in (raw.get('ticked') or []) if t in TICK_NAMES}

    boxes = raw.get('phone_boxes')
    if isinstance(boxes, list):
        phone = ''.join(re.sub(r'\D', '', str(b or '')) for b in boxes)
    else:
        phone = digits(raw.get('phone'))

    heard = None
    for key, value in HEARD_FROM_TICK.items():
        if key in ticked:
            heard = value
            break

    size, size_bad = clean_size(raw.get('size'))
    unreadable = list(raw.get('unreadable') or [])
    if size_bad:
        unreadable.append('size (impossible shape - check the boxes)')

    return {
        'phone': phone,
        'name': raw.get('name'),
        'company': raw.get('company'),
        'vehicle': raw.get('vehicle'),
        'size': size,
        'stock_in': 'in_stock' in ticked,
        'stock_out': 'out_of_stock' in ticked,
        'stock_local': 'local_offer' in ticked,
        'price_ours': raw.get('price_ours'),
        'price_ours_install': 'ours_with_installation' in ticked,
        'price_local': raw.get('price_local'),
        'price_local_install': 'local_with_installation' in ticked,
        'booked': 'booked' in ticked,
        'heard': heard,
        'unreadable': unreadable,
    }


def parse_claude_json(response) -> dict:
    """Pull the JSON out of a Claude response.

    The response can carry several content blocks and the first is not always
    the text block, so never index into content[0] blindly.
    """
    chunks = [b.text for b in response.content
              if getattr(b, 'type', None) == 'text' and getattr(b, 'text', None)]
    raw = "".join(chunks).strip()
    if not raw:
        # 29 August: the first live card came back with no text block at all.
        # The model had spent the whole 900-token budget before it started
        # writing the answer, so the reply carried nothing but reasoning. The
        # budget is bigger now; this message says which of the two it was so
        # the next person does not have to guess.
        kinds = [getattr(b, 'type', '?') for b in response.content]
        raise ValueError(
            f"Claude returned no text. stop_reason={getattr(response, 'stop_reason', '?')}, "
            f"blocks={kinds or 'none'}")
    raw = raw.replace('```json', '').replace('```', '').strip()
    if not raw.startswith('{'):
        start, end = raw.find('{'), raw.rfind('}')
        if start != -1 and end != -1:
            raw = raw[start:end + 1]
    return json.loads(raw)


def read_card(image_data: bytes) -> dict:
    b64 = base64.standard_b64encode(image_data).decode('utf-8')
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=8000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64",
                            "media_type": "image/jpeg", "data": b64}},
                {"type": "text", "text": CARD_PROMPT},
            ],
        }],
    )
    return normalise_card(parse_claude_json(response))


def read_correction(text: str) -> dict:
    response = claude.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=2000,
        messages=[{"role": "user", "content": TEXT_PROMPT + text}],
    )
    return parse_claude_json(response)


# --------------------------------------------------------------------- helpers

def digits(value) -> str:
    return re.sub(r'\D', '', str(value or ''))


def fmt_phone(value) -> str:
    d = digits(value)
    if len(d) == 11 and d.startswith('1'):
        d = d[1:]
    if len(d) == 10:
        return f"({d[0:3]}) {d[3:6]}-{d[6:]}"
    return str(value or '')


def phone_in_call_log(phone: str):
    """Look the number up in the call log, and if it is not there, look for a
    number that is ALMOST it.

    Returns (found, near) where found is True / False / None (check could not
    run), and near is the list of real numbers from the log that differ from
    the card by one or two digits.

    The near list is the point. 29 August the model read 250 571-4654 as
    260 571-4654 - one digit. Saying "no such number" leaves the rep hunting;
    saying "there was a call from 250 571-4654 today, one digit out" hands him
    the answer, and it is a fact from the log, not another guess.
    """
    if not AIRTABLE_TOKEN:
        return None, []
    want = digits(phone)[-10:]
    if len(want) < 10:
        return None, []
    since = (datetime.datetime.now(datetime.timezone.utc)
             - datetime.timedelta(days=CALL_LOOKBACK_DAYS)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = f'https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_CALLS}'
    params = [('pageSize', 100),
              ('returnFieldsByFieldId', 'true'),
              ('fields[]', CALL_PHONE_FIELD),
              ('filterByFormula',
               "IS_AFTER({Date/Time}, DATETIME_PARSE('%s'))" % since)]
    headers = {'Authorization': f'Bearer {AIRTABLE_TOKEN}'}
    seen, offset = set(), None
    try:
        for _ in range(20):
            p = list(params)
            if offset:
                p.append(('offset', offset))
            r = requests.get(url, headers=headers, params=p, timeout=25)
            if r.status_code != 200:
                log.error("Airtable %s: %s", r.status_code, r.text[:200])
                return None, []
            body = r.json()
            for rec in body.get('records', []):
                d = digits(rec.get('fields', {}).get(CALL_PHONE_FIELD))[-10:]
                if len(d) == 10:
                    seen.add(d)
            offset = body.get('offset')
            if not offset:
                break
    except Exception as exc:
        log.error("Call-log check failed: %s", exc)
        return None, []

    if want in seen:
        return True, []

    near = []
    for d in seen:
        wrong = sum(1 for a, b in zip(d, want) if a != b)
        if wrong <= 2:
            near.append(d)
    near.sort(key=lambda d: sum(1 for a, b in zip(d, want) if a != b))
    return False, near[:3]


def contacts_by_name(name: str):
    """People already in Kommo whose name matches the card.

    Added 29 August after Sergey's objection to leaning on the call log alone:
    a customer can walk in off the street having never phoned, and then the
    absence of a call proves nothing. A name that is already in Kommo against
    a DIFFERENT number is the second, independent way to catch wrong digits -
    and it works for walk-ins, where the call log cannot.

    Returns a list of (name, phone) - at most three, so the message stays
    readable.
    """
    name = (name or '').strip()
    if len(name) < 3:
        return []
    out = []
    try:
        data = kommo_get('/contacts', {'query': name, 'limit': 25})
    except Exception as exc:
        log.error("Name search failed: %s", exc)
        return []
    wanted = name.lower()
    for c in data.get('_embedded', {}).get('contacts', []):
        got = (c.get('name') or '').strip()
        if not got:
            continue
        # Kommo's query is loose. Keep only real name matches, so the rep is
        # not shown three strangers and taught to skip the message.
        if wanted not in got.lower() and got.lower() not in wanted:
            continue
        phones = []
        for f in (c.get('custom_fields_values') or []):
            if f.get('field_id') == F_CONTACT_PHONE:
                phones += [v.get('value') for v in (f.get('values') or [])]
        out.append((got, phones[0] if phones else None))
        if len(out) == 3:
            break
    return out


def stock_words(data: dict) -> str:
    parts = []
    if data.get('stock_in'):
        parts.append("in stock")
    if data.get('stock_out'):
        parts.append("out of stock")
    if data.get('stock_local'):
        parts.append("local offer")
    return ", ".join(parts) if parts else "—"


def confirmation_text(data: dict, store: str, call_check=None,
                      name_matches=None) -> str:
    """What the rep reads before anything is written.

    Built as HTML with every value escaped, NOT as Markdown. 29 August: a card
    read fine and then Telegram refused the message with "can\'t find end of
    the entity" - an asterisk or underscore inside a handwritten name is enough
    to break Markdown, and then the whole card is lost over a punctuation mark.
    HTML plus html.escape() cannot be broken by anything the pen writes.

    Handwritten fields are bold, because those are the only ones a machine can
    misread. Ticks are plain: a box is either marked or it is not.
    """
    def e(v):
        return html.escape(str(v))

    def b(v):
        return f"<b>{e(v)}</b>" if v else "—"

    lines = ["<b>Card read. Check it before I save it.</b>", ""]
    lines.append(f"Phone — {b(fmt_phone(data.get('phone')))}")
    lines.append(f"Name — {b(data.get('name'))}")
    if data.get('company'):
        lines.append(f"Company — {b(data.get('company'))}")
    lines.append(f"Vehicle — {b(data.get('vehicle'))}")
    lines.append(f"Size — {b(data.get('size'))}")
    lines.append(f"Stock — {e(stock_words(data))}")

    if data.get('price_ours'):
        tail = " with installation" if data.get('price_ours_install') else ""
        lines.append(f"Our price — <b>${e(data['price_ours'])}</b>{tail}")
    if data.get('price_local'):
        tail = " with installation" if data.get('price_local_install') else ""
        lines.append(f"Local price — <b>${e(data['price_local'])}</b>{tail}")

    lines.append(f"Booked — {'yes' if data.get('booked') else 'no'}")
    lines.append(f"Heard of us — {e(data.get('heard') or '—')}")
    lines.append(f"Store — {e(store) if store else 'NOT SET — tell Sergey'}")

    unread = data.get('unreadable') or []
    if unread:
        lines.append("")
        lines.append("⚠️ Could not read: " + ", ".join(e(u) for u in unread))

    # Two independent ways to catch wrong digits. Neither blocks anything and
    # neither accuses the rep of an error: a walk-in customer never phoned, so
    # a missing call is not proof of anything on its own.
    #
    # Only False is worth showing for the call log. None means the check did
    # not run, and a warning that fires when nothing is known is a warning
    # nobody reads.
    notes = []
    found, near = (call_check if isinstance(call_check, tuple)
                   else (call_check, []))
    if found is False:
        if near:
            spelled = ", ".join(f"<b>{e(fmt_phone(n))}</b>" for n in near)
            notes.append(f"no call from this number, but there was one from "
                         f"{spelled} — one or two digits out. Check the boxes.")
        else:
            notes.append(f"no call to this number in the last "
                         f"{CALL_LOOKBACK_DAYS} days — fine if he walked in")
    want = digits(data.get('phone'))[-10:]
    for got_name, got_phone in (name_matches or []):
        if got_phone and digits(got_phone)[-10:] != want:
            notes.append(f"Kommo already has <b>{e(got_name)}</b> on "
                         f"<b>{e(fmt_phone(got_phone))}</b>")
    if notes:
        lines.append("")
        lines.append("⚠️ " + "\n⚠️ ".join(notes))

    lines.append("")
    lines.append(f"<i>Goes to:</i> {e(stage_name(data))}")
    return "\n".join(lines)


def stage_name(data: dict) -> str:
    sid = pick_stage(data)
    return {STAGE_BOOKED: "Booked for Install",
            STAGE_QUOTE_SENT: "Quote Sent",
            STAGE_IN_CONTACT: "In Contact"}.get(sid, "New Lead")


def pick_stage(data: dict) -> int:
    """Stage = the last action actually completed. Nothing more is claimed.

    Booked ticked -> he is booked. A price written -> we quoted him. Neither ->
    we spoke to him, which is already more than New Lead means.
    """
    if data.get('booked'):
        return STAGE_BOOKED
    if data.get('price_ours') or data.get('price_local'):
        return STAGE_QUOTE_SENT
    return STAGE_IN_CONTACT


# ----------------------------------------------------------------- Kommo calls

def kommo_get(path, params=None):
    r = requests.get(f"{KOMMO_BASE}{path}", headers=HEADERS,
                     params=params or {}, timeout=30)
    if r.status_code == 204:
        return {}
    r.raise_for_status()
    return r.json()


def find_contact_by_phone(phone: str):
    """Return (contact_id, contact_name) or (None, None).

    Kommo's query search is loose, so every candidate is re-checked digit by
    digit against its own phone fields. A near match is not a match.
    """
    want = digits(phone)
    if len(want) < 10:
        return None, None
    want = want[-10:]
    try:
        data = kommo_get('/contacts', {'query': want, 'limit': 50,
                                       'with': 'leads'})
    except Exception as exc:
        log.error("Contact search failed: %s", exc)
        return None, None
    for c in data.get('_embedded', {}).get('contacts', []):
        for f in (c.get('custom_fields_values') or []):
            if f.get('field_id') != F_CONTACT_PHONE:
                continue
            for v in (f.get('values') or []):
                if digits(v.get('value'))[-10:] == want:
                    return c['id'], c.get('name')
    return None, None


def open_lead_for_contact(contact_id: int):
    """An open lead in our pipeline, if the customer already has one.

    A second call about the same tires is not a second lead. It is more
    information on the one that is already open.
    """
    try:
        data = kommo_get(f'/contacts/{contact_id}', {'with': 'leads'})
    except Exception as exc:
        log.error("Contact fetch failed: %s", exc)
        return None
    for lead in data.get('_embedded', {}).get('leads', []):
        try:
            full = kommo_get(f"/leads/{lead['id']}")
        except Exception:
            continue
        if full.get('pipeline_id') != PIPELINE_ID:
            continue
        if full.get('status_id') in (CLOSED_WON, CLOSED_LOST):
            continue
        return full
    return None


def lead_fields(data: dict, store: str) -> list:
    """Only what is on the card. Nothing derived, nothing guessed."""
    fields = []

    if data.get('size'):
        fields.append({"field_id": F_SIZE,
                       "values": [{"value": str(data['size'])}]})

    if data.get('stock_local'):
        fields.append({"field_id": F_TIRE_BRAND,
                       "values": [{"enum_id": E_BRAND_LOCAL}]})

    # "Out of stock" is only recorded as a Result when the customer did NOT
    # book. On a booked deal it would read as "lost because out of stock",
    # which is the opposite of what happened. The fact itself is never lost:
    # the stock line goes into the note either way.
    if data.get('stock_out') and not data.get('booked'):
        fields.append({"field_id": F_RESULT,
                       "values": [{"enum_id": E_RESULT_OUT_OF_STOCK}]})
        fields.append({"field_id": F_WAITING_FOR,
                       "values": [{"enum_id": E_WAITING_OUR_STOCK}]})

    if data.get('price_ours_install') or data.get('price_local_install'):
        fields.append({"field_id": F_INSTALLATION,
                       "values": [{"enum_id": E_INSTALL_YES}]})

    heard = (data.get('heard') or '').lower()
    if heard in SOURCE_ENUM:
        fields.append({"field_id": F_SOURCE,
                       "values": [{"enum_id": SOURCE_ENUM[heard]}]})

    loc = LOCATION_ENUM.get((store or '').lower())
    if loc:
        fields.append({"field_id": F_LOCATION, "values": [{"enum_id": loc}]})

    return fields


def note_text(data: dict, store: str, who: str) -> str:
    parts = ["Call card"]
    if data.get('company'):
        parts.append(f"Company: {data['company']}")
    if data.get('vehicle'):
        parts.append(f"Vehicle: {data['vehicle']}")
    if data.get('price_ours'):
        tail = " (with installation)" if data.get('price_ours_install') else ""
        parts.append(f"Our price, all in: ${data['price_ours']}{tail}")
    if data.get('price_local'):
        tail = " (with installation)" if data.get('price_local_install') else ""
        parts.append(f"Local price, all in: ${data['price_local']}{tail}")
    parts.append(f"Stock: {stock_words(data)}")
    if store:
        parts.append(f"Store: {store}")
    parts.append(f"Card filled by: {who}")
    return "\n".join(parts)


def lead_title(data: dict) -> str:
    title = data.get('name') or 'Unknown'
    if data.get('size'):
        title += f" — {data['size']}"
    return title


def create_task(lead_id: int, data: dict):
    """Every lead leaves here with a next step. No exceptions.

    A lead with no task is exactly the lead this whole project exists to stop
    losing, so this is not optional and it is not conditional.
    """
    tomorrow = datetime.datetime.now(LOCAL_TZ) + datetime.timedelta(days=1)
    due = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
    if data.get('booked'):
        text = "Confirm the booking with the customer"
    elif data.get('stock_out'):
        text = "Tires were not in stock — come back with an answer"
    else:
        text = "Call back — quoted, not booked"
    payload = [{"text": text,
                "complete_till": int(due.timestamp()),
                "entity_id": lead_id,
                "entity_type": "leads",
                "task_type_id": 1}]
    r = requests.post(f"{KOMMO_BASE}/tasks", headers=HEADERS,
                      json=payload, timeout=30)
    log.info("Task due %s local | %s %s",
             due.strftime("%Y-%m-%d %H:%M %Z"), r.status_code, r.text[:200])
    try:
        return r.json()['_embedded']['tasks'][0]['id']
    except Exception:
        return None


def add_note(lead_id: int, text: str):
    payload = [{"note_type": "common", "params": {"text": text}}]
    r = requests.post(f"{KOMMO_BASE}/leads/{lead_id}/notes",
                      headers=HEADERS, json=payload, timeout=30)
    log.info("Note: %s %s", r.status_code, r.text[:200])
    try:
        return r.json()['_embedded']['notes'][0]['id']
    except Exception:
        return None


def record_card_name(contact_id: int, card_name: str, company: str = None):
    """Put the person's name from the card onto the contact.

    This is safe now because the CARD separates the two things, not the bot.
    NAME is the person, COMPANY is the firm, two printed lines. On 31 August
    the same rule renamed the company "CSN Dayton" into a customer - but that
    card had one line for both, so nothing downstream could tell them apart.
    The split is forced on paper, where the rep decides, and not guessed here.

    Rules, still narrow:
      - only a contact created by an integration (created_by == 0) is renamed,
        because that name is a Facebook or chat handle, not a person;
      - a name typed by a human is never touched;
      - the old handle is kept in "Goes by", so nothing is lost;
      - the company never goes near the contact name.

    Returns the previous "Goes by" so undo can put it back.
    """
    card_name = (card_name or '').strip()
    if not card_name or not contact_id:
        return None
    try:
        c = kommo_get(f'/contacts/{contact_id}')
    except Exception as exc:
        log.error("Contact fetch failed: %s", exc)
        return None

    old = (c.get('name') or '').strip()
    if old.lower() == card_name.lower():
        return None

    goes_by_before = None
    for f in (c.get('custom_fields_values') or []):
        if f.get('field_id') == F_CONTACT_GOES_BY:
            goes_by_before = (f.get('values') or [{}])[0].get('value')

    body = {}
    if c.get('created_by') == 0:
        # A handle from a channel. Replace it with the person, keep the handle.
        body["name"] = card_name
        if not goes_by_before and old:
            body["custom_fields_values"] = [
                {"field_id": F_CONTACT_GOES_BY, "values": [{"value": old}]}]
    else:
        # A person typed this name. Record the card's name beside it instead.
        if goes_by_before:
            return None
        body["custom_fields_values"] = [
            {"field_id": F_CONTACT_GOES_BY, "values": [{"value": card_name}]}]

    r = requests.patch(f"{KOMMO_BASE}/contacts/{contact_id}",
                       headers=HEADERS, json=body, timeout=30)
    log.info("Contact %s: was %r, created_by=%s, wrote %s | %s",
             contact_id, old, c.get('created_by'), json.dumps(body)[:200],
             r.status_code)
    if r.status_code not in (200, 201):
        return None
    return {"name": old if c.get('created_by') == 0 else None,
            "goes_by": goes_by_before}


def update_contact_vehicle(contact_id: int, vehicle: str):
    """The vehicle lives on the person, not on the deal.

    A size is what he bought once. A vehicle is what he drives, and it is the
    row in the register of what this region drives — the one asset that cannot
    be bought from anybody.
    """
    before = None
    try:
        c = kommo_get(f'/contacts/{contact_id}')
        for f in (c.get('custom_fields_values') or []):
            if f.get('field_id') == F_CONTACT_VEHICLE:
                before = (f.get('values') or [{}])[0].get('value')
    except Exception as exc:
        log.error("Vehicle read-before failed: %s", exc)

    payload = {"custom_fields_values": [
        {"field_id": F_CONTACT_VEHICLE, "values": [{"value": vehicle}]}]}
    r = requests.patch(f"{KOMMO_BASE}/contacts/{contact_id}",
                       headers=HEADERS, json=payload, timeout=30)
    log.info("Vehicle on %s: was %r now %r | %s", contact_id, before, vehicle,
             r.status_code)
    return before


def do_undo(u: dict) -> str:
    """Put everything back the way the bot found it.

    A rep saves a card by pressing one button, so a rep will sometimes press it
    on a card that was not right. Undo has to be one button too, standing next
    to the first, or it does not exist as far as the shop floor is concerned.
    """
    done = []
    lead_id = u.get('lead_id')

    if u.get('task_id'):
        r = requests.patch(f"{KOMMO_BASE}/tasks/{u['task_id']}",
                           headers=HEADERS,
                           json={"is_completed": True,
                                 "result": {"text": "Card undone"}}, timeout=30)
        if r.status_code in (200, 201):
            done.append("task closed")

    if u.get('created') and lead_id:
        # Kommo's public API cannot delete a lead. Verified 31 August against
        # the live account: DELETE /api/v4/leads -> 405, DELETE
        # /api/v4/leads/{id} -> 405, POST .../delete -> 404. So undo does the
        # next best thing and makes the lead impossible to mistake for work:
        # emptied, closed lost, and named so it sorts to the eye.
        body = {"name": "\u274c DELETE ME - card undone",
                "price": 0, "status_id": CLOSED_LOST}
        r = requests.patch(f"{KOMMO_BASE}/leads/{lead_id}", headers=HEADERS,
                           json=body, timeout=30)
        log.info("Undo mark lead %s -> %s %s", lead_id, r.status_code,
                 r.text[:200])
        if r.status_code in (200, 201):
            done.append("lead emptied and marked DELETE ME (Kommo's API "
                        "cannot delete it - remove it in Kommo)")
        else:
            done.append(f"could not mark the lead ({r.status_code})")
    elif lead_id and u.get('lead_before'):
        before = u['lead_before']
        body = {"status_id": before.get('status_id')}
        body["price"] = before.get('price') or 0
        fields = []
        for f in (before.get('custom_fields_values') or []):
            fields.append({"field_id": f['field_id'],
                           "values": [{k: v for k, v in val.items()
                                       if k in ('value', 'enum_id')}
                                      for val in f.get('values', [])]})
        if fields:
            body["custom_fields_values"] = fields
        r = requests.patch(f"{KOMMO_BASE}/leads/{lead_id}", headers=HEADERS,
                           json=body, timeout=30)
        log.info("Undo restore lead %s -> %s %s", lead_id, r.status_code,
                 r.text[:300])
        if r.status_code in (200, 201):
            done.append("lead put back")

    cid = u.get('contact_id')
    if cid:
        restore = []
        if u.get('contact_vehicle_before') is not None or u.get('contact_vehicle_written'):
            restore.append({"field_id": F_CONTACT_VEHICLE,
                            "values": [{"value": u.get('contact_vehicle_before') or ""}]})
        nb = u.get('contact_name_before') or {}
        if u.get('contact_goes_by_written'):
            restore.append({"field_id": F_CONTACT_GOES_BY,
                            "values": [{"value": nb.get('goes_by') or ""}]})
        body = {}
        if restore:
            body["custom_fields_values"] = restore
        if nb.get('name'):
            body["name"] = nb['name']
        if body:
            r = requests.patch(f"{KOMMO_BASE}/contacts/{cid}", headers=HEADERS,
                               json=body, timeout=30)
            if r.status_code in (200, 201):
                done.append("contact put back")

    if u.get('note_id') and lead_id:
        add_note(lead_id, "The card above was undone by the rep. "
                          "Everything this bot wrote has been reversed.")

    return ", ".join(done) if done else "nothing could be reversed"


def snapshot(lead: dict) -> dict:
    """Everything about a lead that this bot is capable of changing."""
    return {"status_id": lead.get("status_id"),
            "price": lead.get("price"),
            "custom_fields_values": lead.get("custom_fields_values") or []}


def save_to_kommo(data: dict, store: str, who: str) -> tuple:
    """Returns (ok, message, lead_id, lead_link, was_existing, undo).

    The undo record is written as we go, not reconstructed afterwards: the only
    moment the previous values are knowable for certain is before we overwrite
    them.
    """
    undo = {"lead_id": None, "created": False, "lead_before": None,
            "contact_id": None, "contact_vehicle_before": None,
            "contact_goes_by_before": None, "note_id": None, "task_id": None}
    phone = data.get('phone')
    contact_id, contact_name = find_contact_by_phone(phone) if phone else (None, None)

    price = data.get('price_ours') or None
    fields = lead_fields(data, store)
    stage = pick_stage(data)

    existing = open_lead_for_contact(contact_id) if contact_id else None

    if existing:
        # He already has an open lead. This call is more information on it,
        # not a second lead.
        lead_id = existing['id']
        body = {"status_id": stage, "pipeline_id": PIPELINE_ID}
        if price:
            body["price"] = int(price)
        if fields:
            body["custom_fields_values"] = fields
        log.info("PATCH lead %s body=%s", lead_id, json.dumps(body)[:900])
        undo["lead_before"] = snapshot(existing)
        r = requests.patch(f"{KOMMO_BASE}/leads/{lead_id}",
                           headers=HEADERS, json=body, timeout=30)
        log.info("PATCH lead %s -> %s %s", lead_id, r.status_code, r.text[:900])
        if r.status_code not in (200, 201):
            return False, f"Kommo {r.status_code}: {r.text[:200]}", None, None, True, undo
        was_existing = True
    else:
        lead = {"name": lead_title(data),
                "pipeline_id": PIPELINE_ID,
                "status_id": stage}
        if price:
            lead["price"] = int(price)
        if fields:
            lead["custom_fields_values"] = fields

        if contact_id:
            lead["_embedded"] = {"contacts": [{"id": contact_id}]}
            log.info("CREATE lead on contact %s body=%s", contact_id,
                     json.dumps(lead)[:900])
            r = requests.post(f"{KOMMO_BASE}/leads", headers=HEADERS,
                              json=[lead], timeout=30)
        else:
            contact = {"name": data.get('name') or 'Unknown'}
            if phone:
                contact["custom_fields_values"] = [{
                    "field_id": F_CONTACT_PHONE,
                    "values": [{"value": digits(phone), "enum_code": "WORK"}]}]
            lead["_embedded"] = {"contacts": [contact]}
            r = requests.post(f"{KOMMO_BASE}/leads/complex", headers=HEADERS,
                              json=[lead], timeout=30)

        log.info("CREATE lead -> %s %s", r.status_code, r.text[:900])
        if r.status_code not in (200, 201):
            return False, f"Kommo {r.status_code}: {r.text[:200]}", None, None, False, undo

        try:
            result = r.json()
            if isinstance(result, list):
                lead_id = result[0].get('id')
                if not contact_id:
                    contact_id = result[0].get('contact_id')
            else:
                leads = result.get('_embedded', {}).get('leads', [])
                lead_id = leads[0].get('id') if leads else None
        except Exception as exc:
            return False, f"Kommo replied but could not be read: {exc}", None, None, False, undo
        was_existing = False
        undo["created"] = True

    if not lead_id:
        return False, "Kommo did not return a lead id", None, None, was_existing, undo

    undo["lead_id"] = lead_id
    undo["contact_id"] = contact_id
    undo["note_id"] = add_note(lead_id, note_text(data, store, who))
    undo["task_id"] = create_task(lead_id, data)
    if contact_id and data.get('vehicle'):
        undo["contact_vehicle_before"] = update_contact_vehicle(
            contact_id, data['vehicle'])
        undo["contact_vehicle_written"] = True
    if contact_id:
        undo['contact_name_before'] = record_card_name(
            contact_id, data.get('name'), data.get('company'))
        undo['contact_goes_by_written'] = True

    link = f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/detail/{lead_id}"
    return True, "", lead_id, link, was_existing, undo


# -------------------------------------------------------------------- handlers

async def send_html(target, text, **kwargs):
    """Edit a message as HTML, and if Telegram refuses, send it as plain text.

    A card must never be lost over formatting. 29 August: a correctly read card
    died because Telegram rejected the markup, and the rep was told to "recover
    it by hand". Escaping fixes the known cause; this catches the unknown ones.
    """
    try:
        return await target.edit_text(text, parse_mode='HTML', **kwargs)
    except Exception as exc:
        log.error("HTML message refused (%s) - falling back to plain", exc)
        plain = re.sub(r'</?(b|i|code|pre|u|s)>', '', text)
        plain = html.unescape(plain)
        return await target.edit_text(plain, **kwargs)


# ------------------------------------------------------------------- the scans
# Dropbox app "KORS Card Scans", App folder access: the app can see its own
# folder and nothing else in the Dropbox. Files land in
#   Dropbox/Apps/KORS Card Scans/2026-09-01 07-16 Kelowna - Rob Tallman.jpg
# Date first so the folder sorts by itself, then the store, then the customer.

_DBX_TOKEN = {'value': None, 'expires': datetime.datetime.min}

# Dropbox refuses these in a path; a customer name can carry any of them.
BAD_IN_NAME = re.compile(r'[/\\:?*"<>|]')


def dropbox_ready() -> bool:
    return bool(DROPBOX_APP_KEY and DROPBOX_APP_SECRET and DROPBOX_REFRESH_TOKEN)


def dropbox_token() -> str:
    """A short-lived access token, refreshed a minute before it dies."""
    now = datetime.datetime.utcnow()
    if _DBX_TOKEN['value'] and now < _DBX_TOKEN['expires']:
        return _DBX_TOKEN['value']
    r = requests.post('https://api.dropboxapi.com/oauth2/token',
                      data={'grant_type': 'refresh_token',
                            'refresh_token': DROPBOX_REFRESH_TOKEN},
                      auth=(DROPBOX_APP_KEY, DROPBOX_APP_SECRET), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Dropbox refused the refresh token: "
                           f"{r.status_code} {r.text[:200]}")
    body = r.json()
    _DBX_TOKEN['value'] = body['access_token']
    _DBX_TOKEN['expires'] = now + datetime.timedelta(
        seconds=int(body.get('expires_in', 14400)) - 60)
    return _DBX_TOKEN['value']


def scan_filename(data, store, when=None) -> str:
    when = when or datetime.datetime.now(LOCAL_TZ)
    stamp = when.strftime('%Y-%m-%d %H-%M')
    where = store or 'Unknown'
    who = (data.get('name') or data.get('company') or 'No name').strip()
    who = BAD_IN_NAME.sub(' ', who)
    who = re.sub(r'\s+', ' ', who).strip()[:60] or 'No name'
    return f"{stamp} {where} - {who}.jpg"


def upload_scan(image: bytes, filename: str):
    """Put one card in the folder. Returns (ok, path_or_error).

    A failure here must never cost a lead: the caller logs it and carries on.
    """
    if not dropbox_ready():
        return False, "Dropbox not configured"
    try:
        path = '/' + filename
        args = {'path': path, 'mode': 'add', 'autorename': True,
                'mute': True, 'strict_conflict': False}
        r = requests.post(
            'https://content.dropboxapi.com/2/files/upload',
            headers={'Authorization': f'Bearer {dropbox_token()}',
                     'Dropbox-API-Arg': json.dumps(args),
                     'Content-Type': 'application/octet-stream'},
            data=image, timeout=60)
        if r.status_code != 200:
            return False, f"{r.status_code} {r.text[:200]}"
        return True, r.json().get('path_display', path)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def rename_scan(old_path: str, new_filename: str):
    """The rep corrected the name after the read. Move the file to match."""
    if not old_path or not dropbox_ready():
        return False, "nothing to move"
    new_path = '/' + new_filename
    if old_path == new_path:
        return True, old_path
    try:
        r = requests.post(
            'https://api.dropboxapi.com/2/files/move_v2',
            headers={'Authorization': f'Bearer {dropbox_token()}',
                     'Content-Type': 'application/json'},
            json={'from_path': old_path, 'to_path': new_path,
                  'autorename': True}, timeout=30)
        if r.status_code != 200:
            return False, f"{r.status_code} {r.text[:200]}"
        return True, r.json()['metadata'].get('path_display', new_path)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


STORE_BUTTONS = ("Kelowna", "Vernon", "Vancouver Island", "Online")


def keyboard_for(store: str) -> InlineKeyboardMarkup:
    """The store is a fact the card itself does not carry.

    A rep who works one shop has it set once in STORE_BY_USER and never thinks
    about it again. The leads manager takes cards for every shop, so for her it
    is a question, and it is asked here - one tap, on the card in front of her,
    instead of a lead filed against the wrong store.
    """
    picked = (store or '').strip().lower()
    row = [InlineKeyboardButton(("\u25cf " if s.lower() == picked else "") + s,
                                callback_data="store:" + s)
           for s in STORE_BUTTONS]
    return InlineKeyboardMarkup([row[:2], row[2:], [
        InlineKeyboardButton("\u2705 Correct \u2014 save it", callback_data="save"),
        InlineKeyboardButton("\u270f\ufe0f Fix something", callback_data="fix"),
    ]])


def store_for(update: Update) -> str:
    user = update.effective_user
    return STORE_BY_USER.get(str(user.id), '') if user else ''


def who_for(update: Update) -> str:
    u = update.effective_user
    if not u:
        return "unknown"
    return f"@{u.username}" if u.username else u.full_name


async def notify_admin(context, update, error_text: str):
    if not ADMIN_CHAT_ID:
        log.warning("ADMIN_CHAT_ID not set — admin alert skipped")
        return
    try:
        chat = update.effective_chat
        msg = update.effective_message
        where = f"\nMessage: chat {chat.id}, msg {msg.message_id}" if chat and msg else ""
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=("🚨 KORS CARD BOT — LEAD NOT CREATED\n\n"
                  f"From: {who_for(update)}{where}\n\n"
                  f"Error:\n{error_text[:600]}\n\n"
                  "The card was NOT saved. Recover it by hand."))
    except Exception as exc:
        log.error("Admin notify failed: %s", exc, exc_info=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Reading the card…")
    store = store_for(update)
    when = datetime.datetime.now(LOCAL_TZ)

    try:
        photo = update.message.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        image = buf.getvalue()
    except Exception as exc:
        log.error("Could not fetch the photo: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Could not fetch the photo: {exc}")
        await notify_admin(context, update,
                           f"Telegram photo download failed.\n{type(exc).__name__}: {exc}")
        return

    data, read_error = None, None
    try:
        data = read_card(image)
    except Exception as exc:
        read_error = exc
        log.error("Card read failed: %s", exc, exc_info=True)

    # The scan is filed whether or not the reading worked. When it did not, this
    # photograph is the only record of that call that exists anywhere.
    scan_ok, scan_where = upload_scan(
        image, scan_filename(data or {'name': 'UNREAD'}, store, when))
    if scan_ok:
        log.info("Scan filed: %s", scan_where)
    else:
        log.error("Scan NOT filed: %s", scan_where)
    context.user_data['scan_path'] = scan_where if scan_ok else None
    context.user_data['scan_when'] = when

    if data is None:
        await msg.edit_text(f"❌ Could not read the card: {read_error}")
        await notify_admin(
            context, update,
            f"Reading the photo failed.\n{type(read_error).__name__}: {read_error}\n"
            + (f"The photo is filed as {scan_where}" if scan_ok
               else f"The photo was NOT filed either: {scan_where}"))
        return

    check = phone_in_call_log(data.get('phone'))
    matches = contacts_by_name(data.get('name'))

    context.user_data['warn'] = (check, matches)
    context.user_data['pending'] = data
    context.user_data['store'] = store
    context.user_data['awaiting_fix'] = False

    await send_html(msg, confirmation_text(data, store, check, matches),
                    reply_markup=keyboard_for(context.user_data.get('store', '')))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('pending')

    if not data and query.data != 'undo':
        await query.edit_message_text("This card has expired. Send the photo again.")
        return

    if query.data == 'undo':
        u = context.user_data.get('undo')
        if not u:
            await query.edit_message_text(
                query.message.text +
                "\n\n↩️ Too late to undo from here — tell Sergey.")
            return
        await query.edit_message_text("⏳ Putting it back…")
        result = do_undo(u)
        context.user_data['undo'] = None
        await query.edit_message_text(f"↩️ Undone: {result}")
        return

    if query.data == 'fix':
        context.user_data['awaiting_fix'] = True
        # Rebuilt from the data, never re-parsed out of the old message: taking
        # the message back out of Telegram and feeding it in again is how a
        # stray character turns into a lost card.
        await send_html(
            query.message,
            confirmation_text(data, context.user_data.get('store', '')) +
            "\n\n✏️ Send the correction in one message, e.g. "
            "<code>phone 250 571 4654</code> or <code>name Dan Coombs</code>.")
        return

    if query.data.startswith('store:'):
        store = query.data.split(':', 1)[1]
        context.user_data['store'] = store
        warn = context.user_data.get('warn') or (None, [])
        await send_html(query.message,
                        confirmation_text(data, store, warn[0], warn[1]),
                        reply_markup=keyboard_for(store))
        return

    # save
    store = context.user_data.get('store', '')
    if not store:
        await query.message.reply_text(
            "Pick the store first — Kelowna, Vernon, Vancouver Island or "
            "Online. Nothing is saved until the lead has one.")
        return
    await query.edit_message_text("⏳ Saving to Kommo…")
    ok, err, lead_id, link, existing, undo = save_to_kommo(data, store, who_for(update))

    if not ok:
        await query.edit_message_text(f"❌ Kommo refused it:\n{err}")
        await notify_admin(context, update,
                           f"{err}\n\nCard: {json.dumps(data, ensure_ascii=False)}")
        return

    # If the rep corrected the name, the scan is filed under the wrong one.
    # Move it, so the folder and Kommo call the customer the same thing.
    old_scan = context.user_data.get('scan_path')
    if old_scan:
        moved, where = rename_scan(
            old_scan, scan_filename(data, store, context.user_data.get('scan_when')))
        log.info("Scan %s: %s", "moved" if moved else "NOT moved", where)

    context.user_data['pending'] = None
    context.user_data['undo'] = undo
    head = ("♻️ Added to the lead this customer already had"
            if existing else "✅ Lead created")
    await query.edit_message_text(
        f"{head}\n\n{lead_title(data)}\n{stage_name(data)}\n\n{link}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("↩️ Undo — I saved it by mistake",
                                 callback_data="undo")]]))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Text only ever corrects a card that is waiting. It never creates a lead.

    The old bot turned any message into a lead, which is how a lead gets made
    out of "ok thanks".
    """
    if not context.user_data.get('awaiting_fix') or not context.user_data.get('pending'):
        await update.message.reply_text(
            "Send me a photo of a call card. "
            "Text on its own does not create a lead.")
        return

    msg = await update.message.reply_text("⏳ Applying the correction…")
    try:
        patch = read_correction(update.message.text)
        data = dict(context.user_data['pending'])
        data.update({k: v for k, v in patch.items() if v is not None})
        context.user_data['pending'] = data
        context.user_data['awaiting_fix'] = False
        check = phone_in_call_log(data.get('phone'))
        matches = contacts_by_name(data.get('name'))
        context.user_data['warn'] = (check, matches)
        await send_html(
            msg,
            confirmation_text(data, context.user_data.get('store', ''),
                              check, matches),
            reply_markup=keyboard_for(context.user_data.get('store', '')))
    except Exception as exc:
        log.error("Correction failed: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Could not read the correction: {exc}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>KORS Tire — call cards</b>\n\n"
        "Photograph a filled-in call card and send it here.\n"
        "I read it, show you what I read, and save it to Kommo "
        "only after you confirm.\n\n"
        "One card = one lead.",
        parse_mode='HTML')


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    store = store_for(update) or "NOT SET"
    await update.message.reply_text(
        f"User id: <code>{u.id}</code>\n"
        f"Chat id: <code>{update.effective_chat.id}</code>\n"
        f"Store: {html.escape(store)}", parse_mode='HTML')


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []
    try:
        r = claude.messages.create(
            model=CLAUDE_MODEL, max_tokens=1000,
            messages=[{"role": "user",
                       "content": 'Return ONLY this JSON: {"ok": true}'}])
        lines.append(f"✅ Claude OK — {CLAUDE_MODEL} — {parse_claude_json(r)}")
    except Exception as exc:
        lines.append(f"❌ Claude FAILED — {CLAUDE_MODEL} — {type(exc).__name__}: {exc}")
    try:
        kommo_get('/leads', {'limit': 1})
        lines.append("✅ Kommo OK")
    except Exception as exc:
        lines.append(f"❌ Kommo FAILED — {type(exc).__name__}: {exc}")
    if not AIRTABLE_TOKEN:
        lines.append("⚠️ Call-log check OFF — AIRTABLE_TOKEN not set")
    else:
        found, _ = phone_in_call_log('0000000000')
        lines.append("✅ Call log reachable" if found is False
                     else "❌ Call log NOT reachable")
    if not dropbox_ready():
        lines.append("⚠️ Card scans NOT filed — Dropbox keys not set")
    else:
        try:
            dropbox_token()
            lines.append("✅ Dropbox OK — scans are filed")
        except Exception as exc:
            lines.append(f"❌ Dropbox FAILED — {type(exc).__name__}: {exc}")
    lines.append(f"Stores mapped: {len(STORE_BY_USER)}")
    await update.message.reply_text("\n".join(lines))


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", whoami))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Card bot up. Model %s | admin alerts %s | stores %d",
             CLAUDE_MODEL, "ON" if ADMIN_CHAT_ID else "OFF", len(STORE_BY_USER))
    app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    main()
