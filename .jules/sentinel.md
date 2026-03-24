## 2024-05-30 - Security Headers
**Vulnerability:** Missing security headers on FastAPI responses.
**Learning:** Added `SecurityHeadersMiddleware`. Had to be careful with `Content-Security-Policy`. Setting it to `default-src 'self'` broke FastAPI's built-in Swagger UI (`/docs`) because it relies on external CDNs (like `cdn.jsdelivr.net`) for its CSS and JS. It is safer to use a more permissive CSP or omit it if we don't know the full frontend requirements, or just omit CSP and include the other headers (`X-Content-Type-Options`, `X-Frame-Options`, `Strict-Transport-Security`, `X-XSS-Protection`).
**Prevention:** I will omit the `Content-Security-Policy` header to avoid breaking the Swagger UI and any other frontend integrations, and keep the other standard security headers.
