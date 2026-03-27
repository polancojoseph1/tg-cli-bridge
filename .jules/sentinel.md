## 2024-03-27 - Unvalidated Proxy URLs
**Vulnerability:** The proxy endpoints in `v1_api.py` (`api_chat_proxy` and `api_proxy_verify`) accepted external URLs without validating the protocol scheme, creating an SSRF (Server-Side Request Forgery) risk where an attacker could pass a `file://` or `ftp://` scheme and exploit `httpx`.
**Learning:** Even internal routing/proxy endpoints should validate URLs explicitly (ensuring they only accept `http` and `https`), as libraries like `httpx` will throw unhandled exceptions or potentially perform arbitrary local requests if given `file://` protocols.
**Prevention:** Always parse and explicitly whitelist allowed protocols for any externally-provided or configuration-derived URLs before making HTTP requests.
