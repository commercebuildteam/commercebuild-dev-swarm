"""Local authenticated API requests must never follow redirects or proxies."""
import urllib.request,urllib.parse
class NoRedirect(urllib.request.HTTPRedirectHandler):
 def redirect_request(self,req,fp,code,msg,headers,newurl):return None
def validate_url(url):
 parsed=urllib.parse.urlparse(url)
 if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1') or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
  raise ValueError('Server URL must be plain loopback HTTP')
 return url.rstrip('/')
def open_local(request,timeout=5):
 validate_url(urllib.parse.urlunsplit(urllib.parse.urlsplit(request.full_url)._replace(path='',query='',fragment='')))
 return urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=timeout)
