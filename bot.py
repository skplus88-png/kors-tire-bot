import os
import io
import json
import logging
import base64
import requests
import anthropic
from telegram import Update
from telegram.ext import Application, MessageHandler, filters, ContextTypes, CommandHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
KOMMO_TOKEN = os.environ['KOMMO_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
KOMMO_SUBDOMAIN = 'korstire'
KOMMO_BASE = f'https://{KOMMO_SUBDOMAIN}.kommo.com/api/v4'

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

def extract_lead_from_image(image_data: bytes) -> dict:
    b64 = base64.standard_b64encode(image_data).decode('utf-8')
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}
                },
                {
                    "type": "text",
                    "text": """This is a photo from a tire shop employee. It's either:
- A handwritten pink lead sticker with customer info
- A screenshot from Facebook Marketplace, Instagram, or other messaging channel

Extract ALL visible information. Return ONLY valid JSON, no markdown, no extra text:
{
  "name": "customer full name or null",
  "phone": "phone number or null",
  "tire_size": "tire size like 225/65R17 or null",
  "brand": "tire brand and model or null",
  "channel": "one of: Phone, Walk-in, Facebook Marketplace, Facebook, Instagram, Referral, Google, Email, or null",
  "customer_type": "New or Repeat or null",
  "status": "one of: Booked, Will Call Back, Will Come In, Follow Up, Lost, Changeover, or null",
  "appointment": "appointment date and time if mentioned or null",
  "notes": "any other relevant info or null"
}"""
                }
            ]
        }]
    )
    text = response.content[0].text.strip().replace('```json','').replace('```','').strip()
    return json.loads(text)

def extract_lead_from_text(text: str) -> dict:
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=800,
        messages=[{
            "role": "user",
            "content": f"""Extract lead info from this text from a tire shop employee and return ONLY valid JSON:
{{
  "name": "customer name or null",
  "phone": "phone or null",
  "tire_size": "tire size or null",
  "brand": "brand/model or null",
  "channel": "one of: Phone, Walk-in, Facebook Marketplace, Facebook, Instagram, Referral, Google, Email, or null",
  "customer_type": "New or Repeat or null",
  "status": "one of: Booked, Will Call Back, Will Come In, Follow Up, Lost, Changeover, or null",
  "appointment": "appointment time or null",
  "notes": "other info or null"
}}

Text: {text}"""
        }]
    )
    text_resp = response.content[0].text.strip().replace('```json','').replace('```','').strip()
    return json.loads(text_resp)

BRAND_ENUMS = {
    "ultraforce": 836269, "wide climber": 836269,
    "greentrac": 836273, "gt": 836273,
    "centara": 836277,
    "zmaxx": 836281,
    "local": 836283,
    "suretrac": 836275,
    "road cruza": 836279,
}

SOURCE_ENUMS = {
    "repeat": 836285, "repeat customer": 836285,
    "facebook": 836287, "facebook marketplace": 836287, "meta": 836287,
    "referral": 836289, "refferal": 836289,
    "google": 836291,
    "castenet": 836293,
    "walk-in": 836297, "walk in": 836297,
    "instagram": 836287,
    "phone": 836299,
}

TIRE_TYPE_ENUMS = {
    "winter": 836261,
    "all season": 836263, "as": 836263,
    "summer": 836591,
    "all-weather": 836595, "aw": 836595, "all weather": 836595,
    "lt": 836265, "lt truck": 836265,
    "quad": 836593, "atv": 836593,
}

def create_kommo_lead(data: dict) -> tuple[bool, str]:
    name = data.get('name') or 'Unknown'
    tire_size = data.get('tire_size', '')
    brand = data.get('brand', '')
    
    lead_name = name
    if tire_size: lead_name += f" — {tire_size}"
    if brand: lead_name += f" {brand}"

    contact = {"name": name}
    if data.get('phone'):
        contact["custom_fields_values"] = [{
            "field_code": "PHONE",
            "values": [{"value": data['phone'], "enum_code": "WORK"}]
        }]

    # Build custom fields
    custom_fields = []

    # Size field (text)
    if tire_size:
        custom_fields.append({"field_id": 983549, "values": [{"value": tire_size}]})

    # Tire Brand (multiselect)
    if brand:
        brand_lower = brand.lower()
        brand_enum = None
        for key, enum_id in BRAND_ENUMS.items():
            if key in brand_lower:
                brand_enum = enum_id
                break
        if brand_enum:
            custom_fields.append({"field_id": 983835, "values": [{"enum_id": brand_enum}]})

    # Source (multiselect) - from channel and customer_type
    source_enums = []
    channel = (data.get('channel') or '').lower()
    customer_type = (data.get('customer_type') or '').lower()
    if 'repeat' in customer_type:
        source_enums.append({"enum_id": 836285})
    for key, enum_id in SOURCE_ENUMS.items():
        if key in channel and enum_id != 836285:
            source_enums.append({"enum_id": enum_id})
            break
    if source_enums:
        custom_fields.append({"field_id": 983837, "values": source_enums})

    # Tire Type (multiselect) - detect from brand name or notes
    brand_and_notes = f"{brand} {data.get('notes') or ''}".lower()
    tire_type_enum = None
    for key, enum_id in TIRE_TYPE_ENUMS.items():
        if key in brand_and_notes:
            tire_type_enum = enum_id
            break
    if tire_type_enum:
        custom_fields.append({"field_id": 983831, "values": [{"enum_id": tire_type_enum}]})

    notes_parts = []
    if data.get('appointment'): notes_parts.append(f"Appointment: {data['appointment']}")
    if data.get('notes'): notes_parts.append(f"Notes: {data['notes']}")
    if data.get('status'): notes_parts.append(f"Status: {data['status']}")
    note_text = "\n".join(notes_parts)

    payload = [{
        "name": lead_name,
        "pipeline_id": 13510819,
        "custom_fields_values": custom_fields if custom_fields else None,
        "_embedded": {
            "contacts": [contact]
        }
    }]
    
    if note_text:
        payload[0]["_embedded"]["notes"] = [{
            "note_type": "common",
            "params": {"text": note_text}
        }]

    headers = {
        'Authorization': f'Bearer {KOMMO_TOKEN}',
        'Content-Type': 'application/json'
    }

    resp = requests.post(f'{KOMMO_BASE}/leads/complex', headers=headers, json=payload)
    
    if resp.status_code in [200, 201]:
        try:
            result = resp.json()
            if isinstance(result, list):
                lead_id = result[0].get('id') if result else None
            else:
                leads = result.get('_embedded', {}).get('leads', [])
                lead_id = leads[0].get('id') if leads else None
            if lead_id:
                link = f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/detail/{lead_id}"
            else:
                link = f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/"
            return True, link
        except Exception as e:
            return True, f"https://{KOMMO_SUBDOMAIN}.kommo.com/leads/"
    else:
        return False, f"Kommo error {resp.status_code}: {resp.text[:200]}"

def format_response(data: dict, kommo_link: str) -> str:
    lines = ["✅ *Lead created in Kommo!*\n"]
    if data.get('name'): lines.append(f"👤 {data['name']}")
    if data.get('phone'): lines.append(f"📞 {data['phone']}")
    if data.get('tire_size'): lines.append(f"🔧 {data['tire_size']}" + (f" {data['brand']}" if data.get('brand') else ""))
    if data.get('channel'): lines.append(f"📡 {data['channel']}")
    if data.get('customer_type'): lines.append(f"👥 {data['customer_type']}")
    if data.get('status'): lines.append(f"📊 {data['status']}")
    if data.get('appointment'): lines.append(f"📅 {data['appointment']}")
    if data.get('notes'): lines.append(f"📝 {data['notes']}")
    lines.append(f"\n🔗 [Open in Kommo]({kommo_link})")
    return "\n".join(lines)

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Reading photo...")
    try:
        photo = update.message.photo[-1]
        file = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await file.download_to_memory(buf)
        image_data = buf.getvalue()

        data = extract_lead_from_image(image_data)
        success, link = create_kommo_lead(data)

        if success:
            await msg.edit_text(format_response(data, link), parse_mode='Markdown')
        else:
            await msg.edit_text(f"❌ Data read, but Kommo error:\n{link}\n\nData: {json.dumps(data, ensure_ascii=False)}")
    except Exception as e:
        logging.error(f"Photo error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error: {str(e)}")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text.startswith('/'):
        return
    
    msg = await update.message.reply_text("⏳ Processing...")
    try:
        data = extract_lead_from_text(text)
        success, link = create_kommo_lead(data)
        if success:
            await msg.edit_text(format_response(data, link), parse_mode='Markdown')
        else:
            await msg.edit_text(f"❌ Kommo error: {link}")
    except Exception as e:
        logging.error(f"Text error: {e}", exc_info=True)
        await msg.edit_text(f"❌ Error: {str(e)}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Kors Tire Bot*\n\n"
        "Send me:\n"
        "📸 Photo of a lead sticker — I'll create a lead in Kommo\n"
        "📱 Screenshot from FB Marketplace/Instagram — I'll create a lead\n"
        "✍️ Text with customer info — I'll create a lead\n\n"
        "All automatic!",
        parse_mode='Markdown'
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    logging.info("Bot started!")
    app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
