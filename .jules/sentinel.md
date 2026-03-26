## 2024-05-30 - Security Headers
**Vulnerability:** Missing security headers on FastAPI responses.
**Learning:** Added `SecurityHeadersMiddleware`. Had to be careful with `Content-Security-Policy`. Setting it to `default-src 'self'` broke FastAPI's built-in Swagger UI (`/docs`) because it relies on external CDNs (like `cdn.jsdelivr.net`) for its CSS and JS. It is safer to use a more permissive CSP or omit it if we don't know the full frontend requirements, or just omit CSP and include the other headers (`X-Content-Type-Options`, `X-Frame-Options`, `Strict-Transport-Security`, `X-XSS-Protection`).
**Prevention:** I will omit the `Content-Security-Policy` header to avoid breaking the Swagger UI and any other frontend integrations, and keep the other standard security headers.

## 2024-05-31 - DoS Upload Memory Exhaustion
**Vulnerability:** The `/v1/upload` endpoint read the entire file into memory using `await file.read()` without any size limits. This allows an attacker to easily exhaust the server's RAM by uploading a massive file, causing a Denial of Service (DoS).
**Learning:** Even if `tempfile.NamedTemporaryFile` is used, reading the entire file contents directly to memory first defeats the purpose of chunked uploading and opens a DoS vector. Additionally, it is important to be careful with cross-platform lock file deletion in Python: calling `os.remove` inside a `with tempfile...` block will throw a `PermissionError` on Windows since the file is still open.
**Prevention:** I will always use chunked reading (e.g., `await file.read(1024*1024)`) and enforce explicit size limits when handling file uploads. I will also ensure file handles are properly closed before attempting to clean them up.
