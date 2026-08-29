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
- NAME: one handwritten line.
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
  "name": "as written, or null",
  "vehicle": "as written, or null",
  "size": "as written with its separators, e.g. 275/60R20, or null",
  "price_ours": whole number or null,
  "price_local": whole number or null,
  "unreadable": ["names of fields you could not read confidently"]
}

The two examples above are the shape, not the answer. Read the card in front
of you."""

TEXT_PROMPT = """The following is a correction typed by a KORS Tire rep for a
call card that was just read from a photograph. Return ONLY the fields the rep
is correcting, as valid JSON using these exact keys where they apply: phone,
name, vehicle, size, stock_in, stock_out, stock_local, price_ours,
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

    return {
        'phone': phone,
        'name': raw.get('name'),
        'vehicle': raw.get('vehicle'),
        'size': raw.get('size'),
        'stock_in': 'in_stock' in ticked,
        'stock_out': 'out_of_stock' in ticked,
        'stock_local': 'local_offer' in ticked,
        'price_ours': raw.get('price_ours'),
        'price_ours_install': 'ours_with_installation' in ticked,
        'price_local': raw.get('price_local'),
        'price_local_install': 'local_with_installation' in ticked,
        'booked': 'booked' in ticked,
        'heard': heard,
        'unreadable': raw.get('unreadable') or [],
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
        max_tokens=4096,
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
    tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)
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
    log.info("Task: %s %s", r.status_code, r.text[:200])
    return r.status_code in (200, 201)


def add_note(lead_id: int, text: str):
    payload = [{"note_type": "common", "params": {"text": text}}]
    r = requests.post(f"{KOMMO_BASE}/leads/{lead_id}/notes",
                      headers=HEADERS, json=payload, timeout=30)
    log.info("Note: %s %s", r.status_code, r.text[:200])
    return r.status_code in (200, 201)


def update_contact_vehicle(contact_id: int, vehicle: str):
    """The vehicle lives on the person, not on the deal.

    A size is what he bought once. A vehicle is what he drives, and it is the
    row in the register of what this region drives — the one asset that cannot
    be bought from anybody.
    """
    payload = {"custom_fields_values": [
        {"field_id": F_CONTACT_VEHICLE, "values": [{"value": vehicle}]}]}
    r = requests.patch(f"{KOMMO_BASE}/contacts/{contact_id}",
                       headers=HEADERS, json=payload, timeout=30)
    log.info("Vehicle: %s %s", r.status_code, r.text[:200])
    return r.status_code in (200, 201)


def save_to_kommo(data: dict, store: str, who: str) -> tuple:
    """Returns (ok, message, lead_id, lead_link, was_existing)."""
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
        r = requests.patch(f"{KOMMO_BASE}/leads/{lead_id}",
                           headers=HEADERS, json=body, timeout=30)
        if r.status_code not in (200, 201):
            return False, f"Kommo {r.status_code}: {r.text[:200]}", None, None, True
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

        if r.status_code not in (200, 201):
            return False, f"Kommo {r.status_code}: {r.text[:200]}", None, None, False

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
            return False, f"Kommo replied but could not be read: {exc}", None, None, False
        was_existing = False

    if not lead_id:
        return False, "Kommo did not return a lead id", None, None, was_existing

    add_note(lead_id, note_text(data, store, who))
    create_task(lead_id, data)
    if contact_id and data.get('vehicle'):
        update_contact_vehicle(contact_id, data['vehicle'])

    link = f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/detail/{lead_id}"
    return True, "", lead_id, link, was_existing


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


KEYBOARD = InlineKeyboardMarkup([[
    InlineKeyboardButton("✅ Correct — save it", callback_data="save"),
    InlineKeyboardButton("✏️ Fix something", callback_data="fix"),
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
    try:
        photo = update.message.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)

        data = read_card(buf.getvalue())
        store = store_for(update)
        check = phone_in_call_log(data.get('phone'))
        matches = contacts_by_name(data.get('name'))
        context.user_data['pending'] = data
        context.user_data['store'] = store
        context.user_data['awaiting_fix'] = False

        await send_html(msg, confirmation_text(data, store, check, matches),
                        reply_markup=KEYBOARD)
    except Exception as exc:
        log.error("Card read failed: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Could not read the card: {exc}")
        await notify_admin(context, update,
                           f"Reading the photo failed.\n{type(exc).__name__}: {exc}")


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = context.user_data.get('pending')

    if not data:
        await query.edit_message_text("This card has expired. Send the photo again.")
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

    # save
    await query.edit_message_text("⏳ Saving to Kommo…")
    store = context.user_data.get('store', '')
    ok, err, lead_id, link, existing = save_to_kommo(data, store, who_for(update))

    if not ok:
        await query.edit_message_text(f"❌ Kommo refused it:\n{err}")
        await notify_admin(context, update,
                           f"{err}\n\nCard: {json.dumps(data, ensure_ascii=False)}")
        return

    context.user_data['pending'] = None
    head = ("♻️ Added to the lead this customer already had"
            if existing else "✅ Lead created")
    await query.edit_message_text(
        f"{head}\n\n{lead_title(data)}\n{stage_name(data)}\n\n{link}")


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
        await send_html(
            msg,
            confirmation_text(data, context.user_data.get('store', ''),
                              check, matches),
            reply_markup=KEYBOARD)
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
