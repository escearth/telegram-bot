## 2026-08-14 - XSS in WebApp alert tags
**Vulnerability:** Unescaped template literal (`${a.direction}`) in `webapp/app.js` allowed potential Cross-Site Scripting (XSS) when rendering alerts.
**Learning:** Variables interpolated directly into HTML class attributes must be escaped even if they are expected to be internal strings like "above" or "below", as they originate from an external API (`/api/alerts`).
**Prevention:** Always use the `esc()` function or equivalent sanitization for all interpolated variables when dynamically generating HTML from API data.
