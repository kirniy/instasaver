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
from .telegram import Telegram, TelegramError, rich_html
from .store import Store
from .cleanup import cleanup_jobs
from .media import fetch_media, playback, cookie_path, DownloadError, MAX_BYTES, instagram_caption
from .net import start_proxy, validate_url

log=logging.getLogger('instasaver')
MODES={'video':'Видео','document':'Оригинал','zip':'ZIP'}
WELCOME='<b>Instasaver</b>\n\nПришлите ссылку — я сохраню видео и отправлю его сюда.\n\nYouTube · Instagram · TikTok · и другие сайты\nПодписи Instagram тоже сохраняются.\n\n<b>Видео</b> — смотреть прямо в чате.\n<b>Оригинал</b> — скачать без изменений.\n<b>ZIP</b> — получить файлы одним архивом.\n\nВыбрать формат: /settings'


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
        await self.tg.message(job['chat'],text)
        if job['status_message']:
            await self.tg.quiet('deleteMessage',chat_id=job['chat'],message_id=job['status_message'])
        await self.tg.reaction(job['chat'],job['message'],'👍' if state=='done' else '🤔' if state=='failed' else '👌')
        self.last_progress.pop(job['id'],None)
        self.progress_text.pop(job['id'],None)

    async def send_once(self,job,item,kind,path,**extra):
        if self.store.db.execute('SELECT 1 FROM sent WHERE job=? AND item=? AND kind=?',(job['id'],item,kind)).fetchone():
            return
        method,field={'video':('sendVideo','video'),'document':('sendDocument','document'),'photo':('sendPhoto','photo')}[kind]
        result=await self.tg.call(method,chat_id=job['chat'],**{field:path.as_uri()},**extra)
        media=result.get(field,{})
        file_id=(media[-1] if isinstance(media,list) and media else media).get('file_id','')
        self.store.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)',(job['id'],item,kind,file_id,result['message_id']))

    async def heartbeat(self,job,phase):
        tick=0
        while True:
            if tick%3==0 and job['id'] in self.progress_text:
                with contextlib.suppress(TelegramError):
                    await self.tg.draft(job['chat'],job['id'],self.progress_text[job['id']])
            tick+=1
            await self.tg.quiet('sendChatAction',chat_id=job['chat'],action='upload_document' if phase[0]=='upload' else 'upload_video')
            await asyncio.sleep(4)

    async def process(self,job):
        jid=job['id'];folder=self.root/'downloads'/str(jid);folder.mkdir(parents=True,exist_ok=True)
        phase=['download'];pulse=asyncio.create_task(self.heartbeat(job,phase))
        try:
            if shutil.disk_usage(self.root).free<6_000_000_000:
                raise DownloadError('На сервере временно недостаточно места. Попробуйте позже.')
            await self.progress(job,'Открываю ссылку…',True)
            manifest=folder/'manifest.json'
            if manifest.is_file():
                files=[folder/n for n in json.loads(manifest.read_text())]
                if any(not f.is_file() for f in files):
                    manifest.unlink();files=[]
            else:
                files=[]
            if not files:
                # Personal cookies are never used on requests from other users.
                cookies=cookie_path(job['url'],self.secrets) if job['user']==self.owner else None
                first_cookies=cookies if cookies and cookies.name=='instagram.txt' else None
                try:
                    files=await fetch_media(job['url'],folder,self.proxy,lambda s:self.progress(job,s),
                        first_cookies)
                except DownloadError:
                    if not cookies or first_cookies:
                        raise
                    await self.progress(job,'Пробую с вашей сессией…',True)
                    files=await fetch_media(job['url'],folder,self.proxy,lambda s:self.progress(job,s),cookies)
                manifest.write_text(json.dumps([f.name for f in files]))
            post_caption=instagram_caption(folder,job['url'])
            caption_file=folder/'caption.txt'
            if post_caption:
                caption_file.write_text(post_caption+'\n\n'+job['url']+'\n',encoding='utf-8')
            phase[0]='upload'
            if job['mode']=='zip' or len(files)>1:
                await self.progress(job,'Собираю ZIP…',True)
                archive=folder/'Instasaver.zip'
                def pack():
                    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as z:
                        for f in files:
                            z.write(f,f.name)
                        if caption_file.exists():
                            z.write(caption_file,'caption.txt')
                await asyncio.to_thread(pack)
                if archive.stat().st_size>MAX_BYTES:
                    raise DownloadError('Архив больше 1,95 ГБ. Выберите «Оригинал» и отправьте отдельную ссылку.')
                await self.progress(job,'Отправляю архив…',True)
                await self.send_once(job,'archive','document',archive,caption=f'Instasaver · файлов: {len(files)}',disable_content_type_detection=True)
            else:
                f=files[0]
                title=html.escape(f.stem[:180]);caption=f'<b>{title}</b>\n<a href="{html.escape(job["url"],quote=True)}">Источник</a>'
                if job['mode']=='video':
                    await self.progress(job,'Готовлю видео для просмотра…',True)
                    is_photo=f.suffix.lower() in ('.jpg','.jpeg','.png','.webp')
                    if is_photo and f.stat().st_size<10_000_000:
                        try:
                            await self.send_once(job,f.name,'photo',f,caption=caption,parse_mode='HTML')
                        except TelegramError as e:
                            if e.code!=400:
                                raise
                    video,info,thumb=(None,None,None) if is_photo else await playback(f,folder)
                    if video:
                        await self.progress(job,'Отправляю видео…',True)
                        try:
                            await self.send_once(job,f.name,'video',video,caption=caption,parse_mode='HTML',supports_streaming=True,
                                width=info['width'],height=info['height'],duration=info['duration'],**({'thumbnail':thumb.as_uri()} if thumb else {}))
                        except TelegramError as e:
                            if e.code!=400:
                                raise
                            log.warning('Video rejected; delivering original as document')
                await self.progress(job,'Отправляю оригинал…',True)
                await self.send_once(job,f.name,'document',f,caption='Оригинал · без изменений',disable_content_type_detection=True)
            if post_caption:
                for index in range(0,len(post_caption),3000):
                    part=post_caption[index:index+3000]
                    key=f'caption:{index}'
                    if not self.store.db.execute('SELECT 1 FROM sent WHERE job=? AND item=? AND kind=?',(jid,key,'text')).fetchone():
                        result=await self.tg.message(job['chat'],'<b>Подпись к публикации</b>\n\n'+html.escape(part))
                        self.store.execute('INSERT OR REPLACE INTO sent VALUES(?,?,?,?,?)',(jid,key,'text','',result['message_id']))
            await self.finish(job,'<b>Готово.</b> Пришлите следующую ссылку.', 'done')
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
            ('start','Скачать по ссылке'),('settings','Видео, оригинал или ZIP'),('status','Мои загрузки'),('cancel','Отменить загрузки'),('help','Как пользоваться')]])
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
