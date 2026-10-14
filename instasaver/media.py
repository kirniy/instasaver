import asyncio
import json
import math
import os
import re
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit
from .net import validate_url

MAX_BYTES = 1_950_000_000
SAFE_FORMATS = 'mov,matroska,webm,avi,mp3,wav,aac,flac,ogg,image2,jpeg_pipe,png_pipe,webp_pipe,gif'


class DownloadError(Exception):
    pass


async def run_process(args, cwd=None, timeout=1500, on_line=None, max_disk=None):
    proc = await asyncio.create_subprocess_exec(*args, cwd=cwd, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True, limit=1024*1024)
    output = []
    async def read():
        async for raw in proc.stdout:
            line = raw.decode(errors='replace').strip()
            output.append(line)
            if len(output) > 400:
                del output[:100]
            if max_disk and cwd and sum(p.stat().st_size for p in Path(cwd).glob('*') if p.is_file()) > max_disk:
                raise DownloadError('Файл слишком большой. Максимум — 1,95 ГБ.')
            if on_line:
                await on_line(line)
        await proc.wait()
    async def supervise():
        async def guard():
            while True:
                await asyncio.sleep(1)
                if max_disk and cwd and sum(p.stat().st_size for p in Path(cwd).glob('**/*') if p.is_file()) > max_disk:
                    raise DownloadError('Файл слишком большой. Максимум — 1,95 ГБ.')
        tasks=[asyncio.create_task(read()),asyncio.create_task(guard())]
        try:
            done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
    try:
        await asyncio.wait_for(supervise(), timeout)
    except BaseException:
        if proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        raise
    return proc.returncode, '\n'.join(output)


def friendly_error(raw):
    s = raw.lower()
    if any(x in s for x in ('sign in', 'login', 'cookies', 'authentication', 'confirm you’re not', "confirm you're not")):
        return 'Площадка запросила вход или проверку аккаунта. Попробуйте позже или пришлите другую публичную ссылку.'
    if any(x in s for x in ('private', 'not available', 'unavailable', 'removed', '404')):
        return 'Материал удалён, закрыт или недоступен в регионе сервера.'
    if any(x in s for x in ('429', 'rate-limit', 'too many')):
        return 'Площадка временно ограничила скачивания. Попробуйте через несколько минут.'
    if 'unsupported url' in s:
        return 'Этот сайт пока не поддерживается. Пришлите прямую ссылку на видео или публикацию.'
    if 'maximum' in s or 'larger than max' in s:
        return 'Файл слишком большой. Максимум — 1,95 ГБ.'
    return 'Не получилось скачать материал с этой площадки. Проверьте ссылку и попробуйте ещё раз.'


def cookie_path(url, secrets):
    host = (urlsplit(url).hostname or '').lower()
    for site, domains in {'youtube':('youtube.com','youtu.be'), 'instagram':('instagram.com',), 'tiktok':('tiktok.com',), 'twitter':('x.com','twitter.com')}.items():
        if any(host == d or host.endswith('.'+d) for d in domains):
            p = Path(secrets) / (site+'.txt')
            if p.is_file():
                return p


async def download(url, folder, proxy, progress, cookies=None):
    validate_url(url)
    args = [sys.executable, '-m', 'yt_dlp', '--ignore-config', '--no-playlist', '--playlist-end', '10',
        '--no-warnings', '--newline', '--no-quiet', '--progress', '--progress-delta', '2',
        '--progress-template', 'download:PROGRESS|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s',
        '--print', 'after_move:FILE|%(filepath)s', '--no-simulate', '--write-info-json',
        '--retries', '3', '--fragment-retries', '3', '--socket-timeout', '25', '--concurrent-fragments','2',
        '--max-filesize', str(MAX_BYTES), '--max-downloads','10', '--match-filter', '!is_live',
        '--proxy', proxy, '--js-runtimes', 'node', '--format', 'bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b',
        '--merge-output-format','mp4', '-o', str(folder / '%(title).100B [%(id)s].%(ext)s')]
    if cookies:
        # yt-dlp updates its jar; use a job-local copy, never mutate the master.
        import shutil
        jar = folder / 'session.cookies'
        shutil.copyfile(cookies, jar)
        jar.chmod(0o600)
        args += ['--cookies', str(jar)]
    files = []
    async def line(s):
        if s.startswith('PROGRESS|'):
            value=s.split('|',1)[1].replace('|',' · ').replace('NA','…').replace('Unknown B/s','…').replace('Unknown','…').replace('MiB/s','МБ/с').replace('KiB/s','КБ/с').replace('GiB/s','ГБ/с')
            await progress('Скачиваю · ' + value)
        if s.startswith('FILE|'):
            p = Path(s[5:]).resolve()
            if p.parent == folder.resolve() and p.is_file():
                files.append(p)
    code, output = await run_process(args + ['--',url], cwd=folder, on_line=line, max_disk=MAX_BYTES*2+100_000_000)
    if code not in (0,101) or not files:
        raise DownloadError(friendly_error(output))
    for p in files:
        if p.stat().st_size > MAX_BYTES:
            raise DownloadError('Файл слишком большой. Максимум — 1,95 ГБ.')
    return list(dict.fromkeys(files))


def geometry(stream):
    w, h = int(stream.get('width',0)), int(stream.get('height',0))
    sar = stream.get('sample_aspect_ratio','1:1')
    try:
        a,b = map(int,sar.split(':'))
        if a>0 and b>0:
            w = round(w*a/b)
    except (ValueError,ZeroDivisionError):
        pass
    rotation = float(stream.get('tags',{}).get('rotate',0))
    for side in stream.get('side_data_list',[]):
        if 'rotation' in side:
            rotation = float(side['rotation'])
    if round(rotation) % 180:
        w,h = h,w
    return w,h


async def probe(path):
    code, raw = await run_process(['ffprobe','-v','error','-protocol_whitelist','file,pipe','-format_whitelist',SAFE_FORMATS,'-show_streams','-show_format','-of','json',str(path)],timeout=30)
    if code:
        return None
    try:
        data = json.loads(raw)
        video = next(s for s in data['streams'] if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
        w,h = geometry(video)
        duration = float(data.get('format',{}).get('duration',video.get('duration',0)))
        if not math.isfinite(duration):
            duration=0
        audio = [s for s in data['streams'] if s.get('codec_type')=='audio']
        return dict(width=w,height=h,duration=round(duration),stream=video,audio=audio)
    except (ValueError,KeyError,StopIteration):
        return None


async def playback(path, folder, stem=None):
    info = await probe(path)
    if not info or not info['width'] or not info['height']:
        return None,None,None
    s = info['stream']
    simple = (s.get('codec_name')=='h264' and s.get('pix_fmt') in ('yuv420p','yuvj420p')
        and s.get('sample_aspect_ratio','1:1') in ('1:1','N/A')
        and not any(x.get('rotation',0) for x in s.get('side_data_list',[]))
        and not s.get('tags',{}).get('rotate')
        and all(a.get('codec_name')=='aac' for a in info['audio']))
    target = folder / 'preview' / ((stem or 'Для просмотра') + '.mp4')
    target.parent.mkdir(exist_ok=True)
    args=['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist',SAFE_FORMATS,'-i',str(path),'-map','0:v:0','-map','0:a?']
    if simple:
        args += ['-c','copy']
    else:
        args += ['-vf',"scale=w='trunc(iw*sar/2)*2':h='trunc(ih/2)*2',setsar=1",'-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-threads','2','-c:a','aac','-b:a','192k']
    code,_=await run_process(args+['-movflags','+faststart',str(target)],cwd=folder,timeout=1200,max_disk=MAX_BYTES*2+50_000_000)
    if code or target.stat().st_size>MAX_BYTES:
        target.unlink(missing_ok=True)
        return None,None,None
    actual=await probe(target)
    thumb=target.with_suffix('.jpg')
    code,_=await run_process(['ffmpeg','-nostdin','-v','error','-y','-ss','0','-i',str(target),'-frames:v','1','-vf','scale=320:320:force_original_aspect_ratio=decrease','-q:v','5',str(thumb)],timeout=30)
    return target, actual, thumb if code==0 and thumb.stat().st_size<200000 else None


async def gallery(url,folder,proxy,progress,cookies=None):
    """Instagram photo/mixed carousels, limited to a single post."""
    import re
    u=urlsplit(url)
    if not re.match(r'^/(p|reel|reels|tv)/[^/]+',u.path):
        raise DownloadError('Пришлите ссылку на конкретную публикацию, а не профиль.')
    dest=folder/'gallery';dest.mkdir(exist_ok=True)
    args=[sys.executable,'-m','gallery_dl','--config-ignore','--no-colors','--proxy',proxy,
        '--http-timeout','20','--retries','1','--range','1-21','--directory',str(dest),
        '--filename','{num:02}_{filename}.{extension}','--write-metadata',
        '-o','extractor.instagram.videos=true','-o','extractor.instagram.max-posts=1',
        '-o','extractor.skip=false','-o','downloader.part=true']
    if cookies:
        args+=['--cookies',str(cookies)]
    await progress('Скачиваю публикацию…')
    code,raw=await run_process(args+['--',url],cwd=dest,timeout=120,max_disk=MAX_BYTES)
    files=sorted(p for p in dest.iterdir() if p.is_file() and p.suffix.lower() in ('.mp4','.jpg','.jpeg','.png','.webp','.gif','.mov'))
    if code or not files:
        raise DownloadError(friendly_error(raw))
    if len(files)>20:
        raise DownloadError('В публикации больше 20 файлов. Пришлите ссылку на отдельный материал.')
    result=[]
    for f in files:
        target=folder/f.name;f.rename(target);result.append(target)
    return result


async def _direct_file_once(url,folder,proxy,progress):
    import aiohttp
    from urllib.parse import unquote
    from email.message import Message
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=900,connect=25)) as session:
        async with session.get(url,proxy=proxy,allow_redirects=True,max_redirects=6) as r:
            mime=r.headers.get('Content-Type','').split(';')[0].lower()
            if r.status!=200 or not (mime.startswith(('video/','audio/','image/')) or mime in ('application/pdf','application/zip','application/octet-stream')):
                raise DownloadError('Это не прямая ссылка на файл.')
            if int(r.headers.get('Content-Length','0'))>MAX_BYTES:
                raise DownloadError('Файл слишком большой. Максимум — 1,95 ГБ.')
            header=Message();header['Content-Disposition']=r.headers.get('Content-Disposition','')
            name=Path(header.get_filename() or unquote(urlsplit(str(r.url)).path).rsplit('/',1)[-1] or 'download').name
            name=''.join(c for c in name if ord(c)>=32 and c not in '/\\')[:150]
            if name in ('','.', '..'):
                name='download'
            path=folder/name
            count=0;last=0
            with path.open('wb') as f:
                async for chunk in r.content.iter_chunked(262144):
                    count+=len(chunk)
                    if count>MAX_BYTES:
                        raise DownloadError('Файл слишком большой. Максимум — 1,95 ГБ.')
                    f.write(chunk)
                    if count-last>2_000_000:
                        await progress(f'Скачиваю · {count/1_000_000:.1f} МБ');last=count
            if not count:
                raise DownloadError('Сервер вернул пустой файл.')
            return [path]


async def fetch_media(url,folder,proxy,progress,cookies=None):
    host=(urlsplit(url).hostname or '').lower()
    is_ig=host=='instagram.com' or host.endswith('.instagram.com')
    if Path(urlsplit(url).path).suffix.lower() in ('.mp4','.mov','.webm','.mp3','.m4a','.wav','.jpg','.jpeg','.png','.gif','.webp','.pdf','.zip'):
        try:
            return await direct_file(url,folder,proxy,progress)
        except DownloadError:
            pass
    if is_ig and urlsplit(url).path.startswith('/p/'):
        # A mixed carousel must include photos too, not just its video entries.
        return await gallery(url,folder,proxy,progress,cookies)
    try:
        return await download(url,folder,proxy,progress,cookies)
    except DownloadError as original:
        if is_ig:
            return await gallery(url,folder,proxy,progress,cookies)
        # Only attempt direct-file fallback for URLs with a file suffix.
        if Path(urlsplit(url).path).suffix.lower() in ('.mp4','.mov','.webm','.mp3','.m4a','.wav','.jpg','.jpeg','.png','.gif','.webp','.pdf','.zip'):
            return await direct_file(url,folder,proxy,progress)
        raise original


def instagram_caption(folder,url):
    """Read extractor metadata; never include cookies or signed media URLs."""
    host=(urlsplit(url).hostname or '').lower()
    if host!='instagram.com' and not host.endswith('.instagram.com'):
        return ''
    for path in sorted(folder.rglob('*.json')):
        if path.name=='manifest.json' or path.stat().st_size>5_000_000:
            continue
        try:
            data=json.loads(path.read_text())
        except (ValueError,OSError):
            continue
        if not isinstance(data,dict):
            continue
        description=data.get('description') or data.get('caption')
        if isinstance(description,dict):
            description=description.get('text')
        if isinstance(description,str) and description.strip():
            return description.strip()
    return ''


async def direct_file(url,folder,proxy,progress):
    import aiohttp
    for attempt in range(3):
        try:
            return await _direct_file_once(url,folder,proxy,progress)
        except (aiohttp.ClientError,asyncio.TimeoutError):
            if attempt==2:
                raise DownloadError('Сайт не отвечает. Попробуйте отправить ссылку ещё раз.') from None
            await progress('Переподключаюсь к сайту…')
            await asyncio.sleep(2**attempt)


GENERIC_TITLE=('video by ','photo by ','post by ','reel by ','instagram post','tiktok video','untitled')
HANDLE_SITES={'instagram':'https://www.instagram.com/{}/','tiktok':'https://www.tiktok.com/@{}','twitter':'https://x.com/{}'}


def _site(url):
    host=(urlsplit(url).hostname or '').lower().removeprefix('www.').removeprefix('m.')
    for site,domains in {'instagram':('instagram.com',),'tiktok':('tiktok.com',),'twitter':('x.com','twitter.com'),
            'youtube':('youtube.com','youtu.be')}.items():
        if any(host==d or host.endswith('.'+d) for d in domains):
            return site
    return host


def _first_line(text):
    for line in (text or '').splitlines():
        line=re.sub(r'https?://\S+','',line)
        line=re.sub(r'(?:^|\s)[#@][\w.]+','',line).strip(' \t-–—|·:')
        if len(line)>=3:
            return line
    return ''


def _clip(text,limit):
    text=re.sub(r'\s+',' ',text).strip()
    if len(text)<=limit:
        return text
    cut=text[:limit].rsplit(' ',1)[0] if ' ' in text[:limit] else text[:limit]
    return cut.rstrip(' ,.;:-–—')+'…'


def post_info(folder,url):
    """Normalized creator/title/stats from yt-dlp or gallery-dl metadata."""
    site=_site(url)
    data={}
    for path in sorted(Path(folder).rglob('*.json'),key=lambda p:(not p.name.endswith('.info.json'),str(p))):
        if path.name=='manifest.json' or path.stat().st_size>5_000_000:
            continue
        try:
            value=json.loads(path.read_text())
        except (ValueError,OSError):
            continue
        if isinstance(value,dict) and value.get('_type')!='playlist':
            data=value
            break
    def pick(*keys):
        for k in keys:
            v=data.get(k)
            if v not in (None,'','NA') and not isinstance(v,(dict,list)):
                return v
    description=data.get('description') or data.get('caption') or ''
    if isinstance(description,dict):
        description=description.get('text') or ''
    if site=='instagram':
        handle=pick('channel','username','owner_username','uploader_id')
        name=pick('uploader','fullname','full_name')
    elif site=='tiktok':
        handle=pick('uploader','uploader_id','author')
        name=pick('channel','creator','nickname')
    elif site=='twitter':
        handle=pick('uploader_id','author_name','username')
        name=pick('uploader','nick')
    else:
        handle=None
        name=pick('channel','uploader','artist','creator','uploader_id','author','username')
    if handle and not re.fullmatch(r'[\w.\-]{1,40}',str(handle)) or (handle and str(handle).isdigit()):
        handle=None
    handle=str(handle).lstrip('@') if handle else None
    name=str(name).strip() if name else None
    creator=('@'+handle) if handle else name or ''
    profile=pick('uploader_url','channel_url') if site not in HANDLE_SITES else None
    if handle and site in HANDLE_SITES:
        profile=HANDLE_SITES[site].format(handle)
    title=str(pick('track','title','fulltitle') or '')
    if name and title.lower().startswith(name.lower()+' - '):
        title=title[len(name)+3:]
    generic=not title or title.lower().startswith(GENERIC_TITLE) or title==str(data.get('id',''))
    if site in ('instagram','tiktok','twitter') or generic:
        title=_first_line(description) or ('' if generic else title)
    date=pick('upload_date')
    if date and re.fullmatch(r'\d{8}',str(date)):
        date=f'{date[6:8]}.{date[4:6]}.{date[:4]}'
    elif isinstance(data.get('date'),str) and re.match(r'\d{4}-\d\d-\d\d',data['date']):
        date='.'.join(reversed(data['date'][:10].split('-')))
    else:
        date=None
    num=lambda *k:(lambda v:int(v) if isinstance(v,(int,float)) and v>=0 else None)(pick(*k))
    return dict(site=site,creator=creator,name=name if handle and name and name.lower()!=handle.lower() else None,
        profile=profile,title=_clip(title,200),description=description.strip() if isinstance(description,str) else '',
        likes=num('like_count','likes'),views=num('view_count','play_count'),comments=num('comment_count','comments'),
        duration=num('duration'),date=date,artist=pick('artist','creator'),track=pick('track'),
        id=str(pick('id','display_id','post_shortcode','shortcode') or ''))


def nice_name(info,limit=160):
    """Human file stem: creator — title. Never empty, never a path."""
    parts=[p for p in (info.get('creator','').lstrip('@'),_clip(info.get('title') or '',70)) if p]
    stem=' — '.join(parts) or info.get('id') or 'Instasaver'
    stem=re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]+',' ',stem)
    stem=re.sub(r'\s+',' ',stem).strip(' .')
    if len(stem.encode())>limit:
        cut=stem
        while len((cut+'…').encode())>limit:
            cut=cut[:-1]
        stem=(cut.rsplit(' ',1)[0] if ' ' in cut[len(parts[0]) if parts else 0:] else cut).rstrip(' ,.;:-–—')+'…'
    return stem.rstrip(' .') or 'Instasaver'


def rename_files(files,stem):
    """Rename downloads in place; numbered when there are several."""
    out=[]
    for i,f in enumerate(files,1):
        name=f'{stem} {i:02}{f.suffix.lower()}' if len(files)>1 else stem+f.suffix.lower()
        target=f.with_name(name)
        if target!=f:
            if target.exists():
                target=f.with_name(f'{target.stem} {f.stat().st_ino}{target.suffix}')
            f.rename(target)
        out.append(target)
    return out


async def extract_audio(path,folder,stem,info=None):
    target=Path(folder)/'audio'/(stem+'.mp3');target.parent.mkdir(exist_ok=True)
    meta=[]
    if info:
        for key,value in (('title',info.get('track') or info.get('title')),('artist',info.get('artist') or info.get('name') or info.get('creator'))):
            if value:
                meta+=['-metadata',f'{key}={value}']
    code,raw=await run_process(['ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist',SAFE_FORMATS,
        '-i',str(path),'-map','0:a:0','-vn','-c:a','libmp3lame','-q:a','2',*meta,str(target)],cwd=folder,timeout=600,max_disk=MAX_BYTES)
    if code or not target.is_file() or not target.stat().st_size:
        target.unlink(missing_ok=True)
        raise DownloadError('В этом материале нет звука.' if 'matches no streams' in raw else 'Не получилось вытащить звук.')
    return target
