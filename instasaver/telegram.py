import asyncio
import json
import logging
import random
import aiohttp

log = logging.getLogger(__name__)


def rich_html(text):
    return ''.join('<p>' + block.replace('\n','<br>') + '</p>' for block in text.split('\n\n'))


class TelegramError(Exception):
    def __init__(self, code, description):
        self.code = code
        super().__init__(description)


class Telegram:
    def __init__(self, token, base):
        self.url = base.rstrip('/') + '/bot' + token + '/'
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1800, connect=20))
        self.rich = True
        self.drafts = True

    async def close(self):
        await self.session.close()

    async def call(self, method, **params):
        for attempt in range(5):
            try:
                async with self.session.post(self.url + method, json=params, timeout=aiohttp.ClientTimeout(total=1800 if method in ('sendVideo','sendDocument','sendPhoto') else 75,connect=20)) as r:
                    data = await r.json(content_type=None)
                if data.get('ok'):
                    return data['result']
                code = data.get('error_code', r.status)
                if code == 429:
                    await asyncio.sleep(data.get('parameters', {}).get('retry_after', 2) + random.random())
                    continue
                if code >= 500:
                    await asyncio.sleep(min(2 ** attempt, 15) + random.random())
                    continue
                raise TelegramError(code, data.get('description', 'Telegram error'))
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                # A send may have reached Telegram: never blindly duplicate a media upload.
                if method.startswith('send') and method not in ('sendChatAction', 'sendMessageDraft', 'sendRichMessageDraft'):
                    raise TelegramError(0, 'Не удалось подтвердить отправку. Проверьте сообщения перед повтором.') from None
                await asyncio.sleep(min(2 ** attempt, 15) + random.random())
        raise TelegramError(503, 'Telegram временно недоступен. Попробуйте чуть позже.')

    async def quiet(self, method, **params):
        try:
            return await self.call(method, **params)
        except TelegramError as e:
            log.info('Optional Telegram method %s failed: %s', method, e.code)

    async def message(self, chat, text, **params):
        if self.rich:
            try:
                return await self.call('sendRichMessage', chat_id=chat, rich_message={'html': rich_html(text)}, **params)
            except TelegramError as e:
                if e.code not in (400, 404):
                    raise
                self.rich = False
                log.warning('Rich messages unavailable; using HTML fallback')
        return await self.call('sendMessage', chat_id=chat, text=text, parse_mode='HTML', **params)

    async def reaction(self, chat, message, emoji):
        await self.quiet('setMessageReaction', chat_id=chat, message_id=message, reaction=[{'type':'emoji','emoji':emoji}])

    async def draft(self, chat, job, text):
        if not self.drafts:
            return
        try:
            await self.call('sendRichMessageDraft', chat_id=chat, draft_id=job, rich_message={'html':rich_html(text)}, can_stop=True)
        except TelegramError as e:
            if e.code in (400, 404):
                self.drafts = False
