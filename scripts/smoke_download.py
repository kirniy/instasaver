import asyncio,sys
from pathlib import Path
from instasaver.media import download,cookie_path,probe
from instasaver.net import start_proxy
async def main():
 folder=Path(sys.argv[2]).resolve();folder.mkdir(parents=True,exist_ok=True)
 server,proxy=await start_proxy()
 async def progress(s): print(s,flush=True)
 try:
  cookies=cookie_path(sys.argv[1],'/opt/instasaver/secrets/cookies') if '--cookies' in sys.argv else None
  files=await download(sys.argv[1],folder,proxy,progress,cookies)
  for f in files:
   info=await probe(f)
   print('RESULT',f.name,f.stat().st_size,({k:info[k] for k in ('width','height','duration')} if info else 'document'))
 finally:server.close();await server.wait_closed()
asyncio.run(main())
