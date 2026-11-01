"""Private one-time notice asset. No image data or capability tokens in Git."""
import base64
import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

BASE = 'https://line-group-bot-r3yl.onrender.com'


def notice_config(env=None):
    env = os.environ if env is None else env
    path = Path(env.get('MORNING_NOTICE_FILE','/etc/secrets/morning_notice.json'))
    raw = path.read_text(encoding='utf-8') if path.exists() else env.get('MORNING_ONE_TIME_NOTICE','{}')
    value = json.loads(raw)
    if not isinstance(value,dict):raise ValueError('invalid notice')
    if not value:return None
    if not {'from_date','text'} <= set(value) <= {'from_date','text','image'}:raise ValueError('invalid notice')
    if date.fromisoformat(value['from_date']).isoformat() != value['from_date']:raise ValueError('invalid date')
    if not isinstance(value['text'],str) or not 0 < len(value['text'].strip()) <= 1000:raise ValueError('invalid text')
    if 'image' in value:
        image = value['image']
        if not isinstance(image,dict) or set(image) != {'token','expires_at','sha256','data'}:raise ValueError('invalid image')
        if not re.fullmatch(r'[0-9a-f]{64}',image['token']) or not re.fullmatch(r'[0-9a-f]{64}',image['sha256']):raise ValueError('invalid image')
        if datetime.fromisoformat(image['expires_at']).tzinfo is None:raise ValueError('invalid expiry')
    return value


def active_notice(day, first_day=None, env=None):
    value = notice_config(env)
    return value if value and (first_day or day) <= date.fromisoformat(value['from_date']) <= day else None


def image_bytes(value, now=None):
    image = value['image']
    if (now or datetime.now(timezone.utc)) >= datetime.fromisoformat(image['expires_at']):raise ValueError('expired image')
    data = base64.b64decode(image['data'],validate=True)
    if not data.startswith(b'\xff\xd8') or len(data)>5_000_000 or hashlib.sha256(data).hexdigest()!=image['sha256']:raise ValueError('invalid image')
    return data


def served_image(token, env=None, now=None):
    value = notice_config(env)
    if not value or 'image' not in value or not hmac.compare_digest(token,value['image']['token']):raise ValueError('not found')
    return image_bytes(value,now)


def digest_messages(text, day, first_day=None, env=None):
    messages=[{'type':'text','text':text}]
    value = active_notice(day,first_day,env)
    if value and 'image' in value:
        image_bytes(value)  # Fail before any LINE request if the private asset is invalid.
        url=BASE+'/notice-media/'+value['image']['token']+'.jpg'
        messages.append({'type':'image','originalContentUrl':url,'previewImageUrl':url})
    return messages
