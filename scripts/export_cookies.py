"""Export only media-platform cookies from the local Chrome session. Never logs values."""
import os
import sys
from pathlib import Path
from http.cookiejar import MozillaCookieJar
from yt_dlp.cookies import extract_cookies_from_browser

out = Path(sys.argv[1]); out.mkdir(parents=True,exist_ok=True); out.chmod(0o700)
profile = sys.argv[2] if len(sys.argv)>2 else 'Default'
jar = extract_cookies_from_browser('chrome',profile=profile)
for site, domains in {'youtube':('youtube.com','google.com'), 'instagram':('instagram.com',), 'tiktok':('tiktok.com',), 'twitter':('x.com','twitter.com')}.items():
    target=out/(site+'.txt')
    result=MozillaCookieJar(str(target))
    for c in jar:
        host=c.domain.lstrip('.')
        if any(host==d or host.endswith('.'+d) for d in domains):
            result.set_cookie(c)
    if len(result):
        result.save(ignore_discard=True,ignore_expires=True); target.chmod(0o600)
    print(site, 'cookies:',len(result))
