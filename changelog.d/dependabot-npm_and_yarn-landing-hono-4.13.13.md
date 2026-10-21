### Security

- **The web app's development dependencies take hono 4.13.13.** It was 4.13.10, which GHSA-5r4p-p66f-jhc7 affects: `serveStatic` decoded the request path a second time, so a request could get past middleware on static paths. Hono reaches the web app only through its development tooling and is not part of the deployed site.
