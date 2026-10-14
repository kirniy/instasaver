import asyncio
import contextlib
import html
import json
import logging
import os
import re
import shutil
import signal
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl, urlencode
from .telegram import Telegram, TelegramError, rich_html
from .store import Store
from .cleanup import cleanup_jobs
from .media import (fetch_media, playback, cookie_path, DownloadError, MAX_BYTES, instagram_caption,
    post_info, nice_name, rename_files, extract_audio)
from .net import start_proxy, validate_url

log=logging.getLogger('instasaver')
MODES={'video':'Видео','document':'Оригинал','zip':'ZIP','audio':'Звук'}
WELCOME=('<b>Instasaver</b>\n\nПришлите ссылку, а я пришлю сам файл. Без подписей и мусора, чтобы его было удобно пересылать.\n\n'
    'Instagram · TikTok · YouTube · X · VK · Pinterest · и ещё сотни сайтов\n\n'
    '<b>Что приходит</b>\n'
    'Чистое видео или фото, альбомом, если в посте карусель.\n'
    'Оригинал файла, названный по автору и посту.\n'
    'Отдельная карточка: автор, подпись, лайки, кнопка «Звук».\n\n'
    'Повторная ссылка приходит мгновенно.\n\n'
    '<b>Формат по умолчанию</b>: /settings\n'
    '<b>Видео</b> смотреть в чате, <b>Оригинал</b> файлом, <b>ZIP</b> архивом, <b>Звук</b> mp3.')
SEND={'video':('sendVideo','video'),'document':('sendDocument','document'),'photo':('sendPhoto','photo'),'audio':('sendAudio','audio')}
IMAGES=('.jpg','.jpeg','.png','.webp','.gif','.heic')
TRACKING=('utm_','igsh','igshid','vrfl','si','s','t','feature','fbclid','ref','ref_src','ref_url','share','_r','is_from_webapp','sender_device','pp')


def url_key(url):
    """Stable cache key: same post, different share links."""
    u=urlsplit(url)
    host=(u.hostname or '').lower().removeprefix('www.').removeprefix('m.')
    if host in ('instagram.com','x.com','twitter.com','tiktok.com','vm.tiktok.com','threads.net','pinterest.com'):
        query=''
    else:
        query=urlencode(sorted((k,v) for k,v in parse_qsl(u.query) if not k.lower().startswith(TRACKING)))
    host={'twitter.com':'x.com','youtu.be':'youtube.com/watch'}.get(host,host)
    path=u.path.rstrip('/')
    if host=='youtube.com/watch':
        path,query='', 'v='+u.path.strip('/')
    path=re.sub(r'^/(?:[\w.]+/)?reels?/','/reel/',path) if 'instagram' in host else path
    return host+path+('?'+query if query else '')


def file_ids(result):
    for field in ('video','audio','document','animation'):
        if isinstance(result.get(field),dict):
            return [result[field]['file_id']]
    if isinstance(result.get('photo'),list) and result['photo']:
        return [result['photo'][-1]['file_id']]
    return []


def short(n):
    if n is None:
        return None
    for limit,suffix in ((1e9,' млрд'),(1e6,' млн'),(1e3,' тыс')):
        if n>=limit:
            v=n/limit
            return (f'{v:.1f}'.rstrip('0').rstrip('.') if v<100 else f'{v:.0f}').replace('.',',')+suffix
    return str(n)


def clock(seconds):
    h,rest=divmod(int(seconds),3600);m,sec=divmod(rest,60)
    return f'{h}:{m:02}:{sec:02}' if h else f'{m}:{sec:02}'


def card_parts(info,caption,url,limit=3500):
    """Separate post card: who, what, numbers. Long captions continue in follow-ups."""
    e=html.escape
    who=e(info['creator'] or 'Источник')
    if info['profile']:
        who=f'<a href="{e(info["profile"],quote=True)}">{who}</a>'
    head=f'<b>{who}</b>'+(f' · {e(info["name"])}' if info['name'] else '')
    lines=[head]
    social=info['site'] in ('instagram','tiktok','twitter')
    if info['title'] and not (social and caption):
        lines.append(f'<b>{e(info["title"])}</b>')
    stats=[x for x in (
        info['views'] is not None and f'👁 {short(info["views"])}',
        info['likes'] is not None and f'❤️ {short(info["likes"])}',
        info['comments'] is not None and f'💬 {short(info["comments"])}',
        info['duration'] and f'⏱ {clock(info["duration"])}',
        info['date'] and f'📅 {info["date"]}') if x]
    tail='\n\n'+' · '.join(stats) if stats else ''
    caption=(caption or '').strip()
    chunks=[caption[i:i+limit] for i in range(0,len(caption),limit)] if caption else []
    first='\n'.join(lines)
    if chunks:
        first+=f'\n\n<blockquote expandable>{e(chunks[0])}</blockquote>'
    parts=[first+tail]
    parts+=[f'<blockquote expandable>{e(c)}</blockquote>' for c in chunks[1:]]
    return parts


def card_buttons(job,info):
    row=[{'text':'Открыть пост','url':job['url']}]
    if job['mode']!='audio':
        row.append({'text':'Звук mp3','callback_data':f'audio:{job["id"]}','style':'primary'})
    return {'inline_keyboard':[row]}


def caption_text(info,caption,url):
    head=' · '.join(x for x in (info['creator'],info['name'],info['date']) if x)
    return (head+'\n\n' if head else '')+caption+'\n\n'+url+'\n'


def links(message):
    text=message.get('text') or message.get('caption') or ''
    urls=re.findall(r'https?://[^\s<>]+',text)
    for e in message.get('entities',[])+message.get('caption_entities',[]):
        if e['type']=='text_link':
            urls.append(e['url'])
    return list(dict.fromkeys(x.rstrip('.,;!?)]»') for x in urls))


def buttons(job=None):
    return {'inline_keyboard':[[{'text':'Отменить','callback_data':f'cancel:{job}','style':'danger'}]]} if job else {
        'inline_keyboard':[[{'text':name,'callback_data':'mode:'+mode,'style':'primary'} for mode,name in MODES.items()]]}


class App:
    def __init__(self):
        self.root=Path(os.getenv('DATA_DIR','data')).resolve();self.root.mkdir(parents=True,exist_ok=True)
        self.secrets=os.getenv('SECRETS_DIR','secrets')
        self.owner=int(os.environ['OWNER_ID'])
        self.allowed={int(x) for x in os.getenv('ALLOWED_USER_IDS','').split(',') if x.strip()}
        self.store=Store(self.root/'state.sqlite3')
        self.tg=Telegram(os.environ['BOT_TOKEN'],os.getenv('BOT_API_URL','http://127.0.0.1:8081'))
        self.active={}
        self.stop=asyncio.Event()
        self.last_progress={}
        self.progress_text={}
        self.last_error={}
        self.partial=False
        self.delivery=[]

    async def handle(self,u):
        if 'callback_query' in u:
            c=u['callback_query'];user=c['from']['id'];data=c.get('data','')
            await self.tg.quiet('answerCallbackQuery',callback_query_id=c['id'])
            if self.allowed and user not in self.allowed:
                return
            if data.startswith('mode:') and data[5:] in MODES:
                mode=data[5:];self.store.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(user,mode))
                await self.tg.message(user,f'Готово. Формат: <b>{MODES[mode]}</b>.\nТеперь пришлите ссылку.')
            elif data.startswith('cancel:') and data[7:].isdigit():
                await self.cancel(int(data[7:]),user)
            elif data.startswith('audio:') and data[6:].isdigit():
                await self.audio(int(data[6:]),user,u['update_id'])
            return
        if 'stopped_message_generation' in u:
            s=u['stopped_message_generation'];job=s.get('draft_id');user=s.get('chat',{}).get('id')
            if job and user:
                await self.cancel(job,user)
            return
        m=u.get('message')
        if not m or m.get('from',{}).get('is_bot'):
            return
        user=m['from']['id'];chat=m['chat']['id']
        if m['chat']['type']!='private':
            return
        if self.allowed and user not in self.allowed:
            await self.tg.message(chat,'Этот Instasaver доступен владельцу бота.')
            return
        text=m.get('text','').strip()
        command=text.split()[0].split('@')[0] if text else ''
        if command in ('/start','/help'):
            await self.tg.message(chat,WELCOME)
            return
        if command=='/settings':
            await self.tg.message(chat,f'Формат по умолчанию: <b>{MODES[self.store.mode(user)]}</b>.',reply_markup=buttons())
            return
        if command=='/cancel':
            jobs=self.store.db.execute("SELECT id FROM jobs WHERE user=? AND state IN ('queued','running')",(user,)).fetchall()
            for j in jobs:
                await self.cancel(j['id'],user)
            if not jobs:
                await self.tg.message(chat,'Сейчас нет активных загрузок.')
            return
        if command=='/status':
            jobs=self.store.db.execute("SELECT id,state FROM jobs WHERE user=? AND state IN ('queued','running')",(user,)).fetchall()
            await self.tg.message(chat,'\n'.join(f'#{j["id"]} · '+('скачиваю' if j['state']=='running' else 'в очереди') for j in jobs) or 'Всё готово. Пришлите новую ссылку.')
            return
        urls=links(m)
        if not urls:
            await self.tg.message(chat,'Пришлите <b>ссылку</b> на видео или публикацию. Можно просто переслать сообщение со ссылкой.')
            return
        if len(urls)>5:
            await self.tg.message(chat,'Отправьте до пяти ссылок за раз.')
            return
        pending=self.store.db.execute("SELECT count(*) FROM jobs WHERE user=? AND state IN ('queued','running')",(user,)).fetchone()[0]
        total=self.store.db.execute("SELECT count(*) FROM jobs WHERE state IN ('queued','running')").fetchone()[0]
        if pending+len(urls)>5 or total+len(urls)>30:
            await self.tg.message(chat,'Очередь заполнена. Дождитесь текущих загрузок или нажмите /cancel.')
            return
        recent=self.store.db.execute('SELECT count(*) FROM jobs WHERE user=? AND created>?',(user,time.time()-3600)).fetchone()[0]
        if recent>=30:
            await self.tg.message(chat,'За час уже принято 30 ссылок. Попробуйте позже.')
            return
        for url in urls:
            try:
                validate_url(url)
            except ValueError as e:
                await self.tg.message(chat,html.escape(str(e)))
                continue
            job=self.store.add(u['update_id'],m,url)
            if not job['status_message']:
                status=await self.tg.message(chat,'<b>Ссылка принята</b>\nСейчас скачаю и отправлю сюда.',
                    reply_parameters={'message_id':m['message_id'],'allow_sending_without_reply':True},reply_markup=buttons(job['id']))
                self.store.execute('UPDATE jobs SET status_message=? WHERE id=?',(status['message_id'],job['id']))
                await self.tg.reaction(chat,m['message_id'],'👀')

    async def audio(self,job_id,user,update_id):
        source=self.store.db.execute('SELECT * FROM jobs WHERE id=? AND user=?',(job_id,user)).fetchone()
        if not source:
            return
        busy=self.store.db.execute("SELECT count(*) FROM jobs WHERE user=? AND state IN ('queued','running')",(user,)).fetchone()[0]
        if busy>=5:
            await self.tg.message(source['chat'],'Очередь заполнена. Дождитесь текущих загрузок.')
            return
        m={'chat':{'id':source['chat']},'from':{'id':user},'message_id':source['message']}
        job=self.store.add(update_id,m,source['url'],'audio')
        if not job['status_message']:
            status=await self.tg.message(source['chat'],'<b>Достаю звук</b>\nСейчас пришлю mp3.',
                reply_parameters={'message_id':source['message'],'allow_sending_without_reply':True},reply_markup=buttons(job['id']))
            self.store.execute('UPDATE jobs SET status_message=? WHERE id=?',(status['message_id'],job['id']))

    async def cancel(self,job_id,user):
        job=self.store.db.execute('SELECT * FROM jobs WHERE id=? AND user=?',(job_id,user)).fetchone()
        if not job or job['state'] not in ('queued','running'):
            return
        self.store.execute("UPDATE jobs SET state='cancelled' WHERE id=?",(job_id,))
        if job_id in self.active:
            self.active[job_id].cancel()
        else:
            await self.finish(job,'Загрузка отменена.','cancelled')

    async def progress(self,job,text,force=False):
        now=time.monotonic();jid=job['id']
        if not force and now-self.last_progress.get(jid,0)<4:
            return
        self.last_progress[jid]=now
        escaped='<b>'+html.escape(text)+'</b>'
        self.progress_text[jid]=escaped
        try:
            await self.tg.draft(job['chat'],jid,escaped)
            await self.tg.quiet('editMessageText',chat_id=job['chat'],message_id=job['status_message'],
                rich_message={'html':rich_html(escaped)},reply_markup=buttons(jid))
        except TelegramError:
            pass

    async def finish(self,job,text,state):
        self.store.execute('UPDATE jobs SET state=? WHERE id=?',(state,job['id']))
        if text:
            await self.tg.message(job['chat'],text)
        if job['status_message']:
            await self.tg.quiet('deleteMessage',chat_id=job['chat'],message_id=job['status_message'])
        await self.tg.reaction(job['chat'],job['message'],'👍' if state=='done' else '🤔' if state=='failed' else '👌')
        self.last_progress.pop(job['id'],None)
        self.progress_text.pop(job['id'],None)

    async def send_once(self,job,item,kind,path,**extra):
        if self.store.db.execute('SELECT 1 FROM sent WHERE job=? AND item=? AND kind=?',(job['id'],item,kind)).fetchone():
            self.partial=True
            return
        method,field=SEND[kind]
        result=await self.tg.call(method,chat_id=job['chat'],**{field:path.as_uri() if isinstance(path,Path) else path},**extra)
        file_id=file_ids(result)[0] if file_ids(result) else ''
        self.store.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)',(job['id'],item,kind,file_id,result['message_id']))
        if file_id:
            self.delivery.append([kind,file_id,{k:v for k,v in extra.items() if k in ('width','height','duration','supports_streaming','title','performer')}])

    async def send_album(self,job,index,media):
        key=f'album:{index}'
        if self.store.db.execute('SELECT 1 FROM sent WHERE job=? AND item=? AND kind=?',(job['id'],key,'album')).fetchone():
            self.partial=True
            return
        result=await self.tg.call('sendMediaGroup',chat_id=job['chat'],media=media)
        ids=[file_ids(m)[0] if file_ids(m) else '' for m in result]
        self.store.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)',(job['id'],key,'album',json.dumps(ids),result[0]['message_id']))
        if all(ids):
            self.delivery.append(['album',[{'type':m['type'],'media':i} for m,i in zip(media,ids)],{}])

    async def send_text(self,job,key,text,**extra):
        if self.store.db.execute('SELECT 1 FROM sent WHERE job=? AND item=? AND kind=?',(job['id'],key,'text')).fetchone():
            self.partial=True
            return
        result=await self.tg.call('sendMessage',chat_id=job['chat'],text=text,parse_mode='HTML',
            link_preview_options={'is_disabled':True},**extra)
        self.store.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)',(job['id'],key,'text','',result['message_id']))

    async def send_card(self,job,info,parts):
        for i,part in enumerate(parts):
            await self.send_text(job,'card' if i==0 else f'caption:{i}',part,**({'reply_markup':card_buttons(job,info)} if i==0 else {}))

    async def replay(self,job,payload):
        """Resend a cached delivery by file_id: instant, no download."""
        for kind,ref,extra in payload['items']:
            if kind=='album':
                await self.tg.call('sendMediaGroup',chat_id=job['chat'],media=ref)
            else:
                method,field=SEND[kind]
                await self.tg.call(method,chat_id=job['chat'],**{field:ref},**extra)
        info=payload['info']
        for i,part in enumerate(payload['card']):
            await self.tg.call('sendMessage',chat_id=job['chat'],text=part,parse_mode='HTML',link_preview_options={'is_disabled':True},
                **({'reply_markup':card_buttons(job,info)} if i==0 else {}))

    async def heartbeat(self,job,phase):
        tick=0
        while True:
            if tick%3==0 and job['id'] in self.progress_text:
                with contextlib.suppress(TelegramError):
                    await self.tg.draft(job['chat'],job['id'],self.progress_text[job['id']])
            tick+=1
            await self.tg.quiet('sendChatAction',chat_id=job['chat'],action='upload_document' if phase[0]=='upload' else 'upload_video')
            await asyncio.sleep(4)

    async def download(self,job,folder):
        manifest=folder/'manifest.json'
        if manifest.is_file():
            data=json.loads(manifest.read_text())
            files=[folder/n for n in data['files']]
            if all(f.is_file() for f in files):
                return files,data['private']
            manifest.unlink()
        # Personal cookies are never used on requests from other users.
        cookies=cookie_path(job['url'],self.secrets) if job['user']==self.owner else None
        first_cookies=cookies if cookies and cookies.name=='instagram.txt' else None
        # Expired/challenged cookies must not block public posts: try both ways.
        attempts=[first_cookies,None] if first_cookies else [None,cookies] if cookies else [None]
        for i,jar in enumerate(attempts):
            try:
                files=await fetch_media(job['url'],folder,self.proxy,lambda s:self.progress(job,s),jar)
                break
            except DownloadError:
                if i==len(attempts)-1:
                    raise
                log.warning('Job %s failed with%s cookies, retrying with%s',job['id'],'' if jar else 'out','' if attempts[i+1] else 'out')
                await self.progress(job,'Пробую другим способом…',True)
        files=rename_files(files,nice_name(post_info(folder,job['url'])))
        manifest.write_text(json.dumps({'files':[f.name for f in files],'private':bool(jar)}))
        return files,bool(jar)

    async def process(self,job):
        jid=job['id'];folder=self.root/'downloads'/str(jid);folder.mkdir(parents=True,exist_ok=True)
        phase=['download'];pulse=asyncio.create_task(self.heartbeat(job,phase))
        self.partial=False;self.delivery=[]
        key=url_key(job['url'])
        try:
            hit=None if self.store.db.execute('SELECT 1 FROM sent WHERE job=?',(jid,)).fetchone() else self.store.cached(key,job['mode'],job['user'])
            if hit:
                try:
                    phase[0]='upload'
                    await self.progress(job,'Уже скачивал это. Отправляю сразу…',True)
                    await self.replay(job,hit)
                    await self.finish(job,None,'done')
                    return
                except TelegramError as e:
                    if e.code!=400:
                        raise
                    log.warning('Job %s cache replay rejected; downloading again',jid)
                    self.store.forget(key,job['mode'])
            if shutil.disk_usage(self.root).free<6_000_000_000:
                raise DownloadError('На сервере временно недостаточно места. Попробуйте позже.')
            await self.progress(job,'Открываю ссылку…',True)
            files,private=await self.download(job,folder)
            info=post_info(folder,job['url']);stem=nice_name(info)
            post_caption=info['description'] or instagram_caption(folder,job['url'])
            phase[0]='upload'
            if job['mode']=='audio':
                source=next((f for f in files if f.suffix.lower() not in IMAGES),None)
                if not source:
                    raise DownloadError('В этой публикации только фото, звука нет.')
                await self.progress(job,'Вытаскиваю звук…',True)
                track=await extract_audio(source,folder,stem,info)
                await self.progress(job,'Отправляю звук…',True)
                await self.send_once(job,track.name,'audio',track,title=(info['track'] or info['title'] or stem)[:64],
                    performer=(info['artist'] or info['name'] or info['creator'] or '')[:64],**({'duration':info['duration']} if info['duration'] else {}))
            elif job['mode']=='zip' or (job['mode']=='document' and len(files)>1):
                await self.progress(job,'Собираю ZIP…',True)
                archive=folder/(stem+'.zip')
                def pack():
                    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
                        for f in files:
                            z.write(f,f.name)
                        if post_caption:
                            z.writestr('caption.txt',caption_text(info,post_caption,job['url']))
                await asyncio.to_thread(pack)
                if archive.stat().st_size>MAX_BYTES:
                    raise DownloadError('Архив больше 1,95 ГБ. Выберите «Оригинал» и отправьте отдельную ссылку.')
                await self.progress(job,'Отправляю архив…',True)
                await self.send_once(job,'archive','document',archive,disable_content_type_detection=True)
            elif job['mode']=='video' and len(files)>1:
                await self.progress(job,'Собираю альбом…',True)
                media=[];loose=[]
                for f in files:
                    if f.suffix.lower() in IMAGES and f.stat().st_size<10_000_000 and f.suffix.lower()!='.gif':
                        media.append({'type':'photo','media':f.as_uri()})
                        continue
                    video,vinfo,_=(None,None,None) if f.suffix.lower() in IMAGES else await playback(f,folder,f.stem)
                    if video:
                        media.append({'type':'video','media':video.as_uri(),'supports_streaming':True,
                            'width':vinfo['width'],'height':vinfo['height'],'duration':vinfo['duration']})
                    else:
                        loose.append(f)
                await self.progress(job,'Отправляю альбом…',True)
                for index in range(0,len(media),10):
                    chunk=media[index:index+10]
                    if len(chunk)==1:
                        m=chunk[0];await self.send_once(job,f'single:{index}',m['type'],m['media'],**{k:v for k,v in m.items() if k not in ('type','media')})
                    else:
                        await self.send_album(job,index,chunk)
                for f in loose:
                    await self.send_once(job,f.name,'document',f,disable_content_type_detection=True)
            else:
                f=files[0]
                if job['mode']=='video':
                    await self.progress(job,'Готовлю видео для просмотра…',True)
                    is_photo=f.suffix.lower() in IMAGES
                    if is_photo and f.stat().st_size<10_000_000:
                        try:
                            await self.send_once(job,f.name,'photo',f)
                        except TelegramError as e:
                            if e.code!=400:
                                raise
                    video,vinfo,thumb=(None,None,None) if is_photo else await playback(f,folder,f.stem)
                    if video:
                        await self.progress(job,'Отправляю видео…',True)
                        try:
                            await self.send_once(job,f.name,'video',video,supports_streaming=True,
                                width=vinfo['width'],height=vinfo['height'],duration=vinfo['duration'],**({'thumbnail':thumb.as_uri()} if thumb else {}))
                        except TelegramError as e:
                            if e.code!=400:
                                raise
                            log.warning('Video rejected; delivering original as document')
                await self.progress(job,'Отправляю оригинал…',True)
                await self.send_once(job,f.name,'document',f,disable_content_type_detection=True)
            parts=card_parts(info,post_caption,job['url'])
            await self.send_card(job,info,parts)
            if not self.partial and self.delivery:
                self.store.remember(key,job['mode'],job['user'],private,{'items':self.delivery,'card':parts,'info':info})
            await self.finish(job,None,'done')
            shutil.rmtree(folder,ignore_errors=True)
        except asyncio.CancelledError:
            state=self.store.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()[0]
            if state=='cancelled':
                await self.finish(job,'Загрузка отменена.','cancelled');shutil.rmtree(folder,ignore_errors=True)
            else:
                self.store.execute("UPDATE jobs SET state='queued' WHERE id=?",(jid,))
            raise
        except (DownloadError,TelegramError,asyncio.TimeoutError) as e:
            text=str(e) if not isinstance(e,asyncio.TimeoutError) else 'Загрузка заняла слишком много времени. Попробуйте ещё раз.'
            log.warning('Job %s failed (%s): %s',jid,type(e).__name__,text)
            self.store.execute('UPDATE jobs SET error=? WHERE id=?',(text,jid))
            with contextlib.suppress(TelegramError):
                await self.finish(job,html.escape(text),'failed')
            self.store.execute("UPDATE jobs SET state='failed' WHERE id=?",(jid,))
            shutil.rmtree(folder,ignore_errors=True)
        except Exception as e:
            log.error('Job %s unexpected failure: %s',jid,type(e).__name__)
            self.store.execute('UPDATE jobs SET error=? WHERE id=?',(type(e).__name__,jid))
            self.store.execute("UPDATE jobs SET state='failed' WHERE id=?",(jid,))
            with contextlib.suppress(TelegramError):
                await self.finish(job,'Не удалось завершить загрузку. Попробуйте ещё раз.','failed')
            shutil.rmtree(folder,ignore_errors=True)
        finally:
            state=self.store.db.execute('SELECT state FROM jobs WHERE id=?',(jid,)).fetchone()
            if state and state[0] in ('done','failed','cancelled'):
                shutil.rmtree(folder,ignore_errors=True)
            pulse.cancel()
            await asyncio.gather(pulse,return_exceptions=True)

    async def worker(self):
        # One download at a time keeps the shared VPS responsive and progress unambiguous.
        while not self.stop.is_set():
            job=self.store.db.execute("SELECT * FROM jobs WHERE state='queued' AND status_message IS NOT NULL ORDER BY id LIMIT 1").fetchone()
            if not job:
                await asyncio.sleep(.3);continue
            self.store.execute("UPDATE jobs SET state='running' WHERE id=?",(job['id'],))
            task=asyncio.create_task(self.process(job));self.active[job['id']]=task
            try:
                await task
            except asyncio.CancelledError:
                if self.stop.is_set():
                    raise
            finally:
                self.active.pop(job['id'],None)

    async def dispatch(self):
        while not self.stop.is_set():
            for row in self.store.pending():
                try:
                    await self.handle(json.loads(row['body']))
                except TelegramError as e:
                    log.warning('Update %s Telegram failure %s',row['id'],e.code)
                    if e.code in (429,503):
                        await asyncio.sleep(3);break
                except Exception as e:
                    log.error('Update %s failed: %s',row['id'],type(e).__name__)
                self.store.execute('UPDATE inbox SET done=1,body=? WHERE id=?',('{}',row['id']))
            await asyncio.sleep(.2)

    async def janitor(self):
        while not self.stop.is_set():
            try:
                count=cleanup_jobs(self.store,self.root,self.active)
                if count:
                    log.info('Removed %s orphan job folders',count)
            except OSError as e:
                log.warning('Cleanup will retry: %s',type(e).__name__)
            await asyncio.sleep(300)

    async def poll(self):
        while not self.stop.is_set():
            try:
                updates=await self.tg.call('getUpdates',offset=self.store.offset(),timeout=30,
                    allowed_updates=['message','callback_query','stopped_message_generation'])
                self.store.ingest(updates)
                (self.root/'healthy').touch()

            except TelegramError as e:
                log.warning('Polling failure %s',e.code);await asyncio.sleep(5)

    async def run(self):
        self.proxy_server,self.proxy=await start_proxy()
        me=await self.tg.call('getMe')
        log.info('Started @%s',me['username'])
        await self.tg.call('deleteWebhook',drop_pending_updates=False)
        await self.tg.call('setMyCommands',commands=[{'command':c,'description':d} for c,d in [
            ('start','Скачать по ссылке'),('settings','Видео, оригинал, ZIP или звук'),('status','Мои загрузки'),('cancel','Отменить загрузки'),('help','Как пользоваться')]])
        for sig in (signal.SIGTERM,signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig,self.stop.set)
        tasks=[asyncio.create_task(f()) for f in (self.worker,self.dispatch,self.poll,self.janitor)]
        stop_task=asyncio.create_task(self.stop.wait())
        done,_=await asyncio.wait([*tasks,stop_task],return_when=asyncio.FIRST_COMPLETED)
        self.stop.set()
        for t in tasks:
            t.cancel()
        stop_task.cancel()
        results=await asyncio.gather(*tasks,return_exceptions=True)
        self.proxy_server.close();await self.proxy_server.wait_closed();await self.tg.close()
        for result in results:
            if isinstance(result,Exception):
                raise result


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    async def serve():
        await App().run()
    asyncio.run(serve())

if __name__=='__main__':
    main()
