### Security

- **The web app takes next 16.4.2.** It was 16.3.8, which GHSA-2464-fhr4-w9xw (critical: remote code execution through the Image Optimization API when SVG files are used) and GHSA-p3m5-59mp-jv5r (high: denial of service in the App Router through Server Actions) affect; 16.4.1 fixes both. eslint-config-next moves to 16.4.2 with it.
- **The web app takes dompurify 3.4.17.** It was 3.4.16, which GHSA-cg2g-xh64-v4q7 (critical: template expressions could survive sanitizing in shadow-root content), GHSA-mrv2-3pqr-j5jr (high: a hook that removed a parent or sibling element could leave event handlers in place) and GHSA-c6gr-qvv4-5v3f (low: a check on SMIL href attributes was skipped for XHTML input) affect; 3.4.17 fixes all three. It reaches the web app through posthog-js.
