"""Public resolvers for the demo scripts: the local network's DNS intermittently fails (SERVFAIL)."""
import socket, functools
import dns.resolver, dns.asyncresolver
for mod in (dns.resolver, dns.asyncresolver):
    r = mod.Resolver(configure=False); r.nameservers = ["8.8.8.8", "1.1.1.1"]; mod.default_resolver = r
_orig = socket.getaddrinfo
@functools.lru_cache(maxsize=256)
def _lookup(host):
    try:
        return [a.address for a in dns.resolver.resolve(host, "A")]
    except Exception:
        return []
def _getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    try:
        return _orig(host, port, family, type, proto, flags)
    except socket.gaierror:
        ips = _lookup(host) if isinstance(host, str) else []
        if not ips: raise
        return [r for ip in ips for r in _orig(ip, port, family, type, proto, flags)]
socket.getaddrinfo = _getaddrinfo
