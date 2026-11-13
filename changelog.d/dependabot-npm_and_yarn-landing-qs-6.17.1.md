### Security

- **The web app's development dependencies take qs 6.17.1.** It was 6.16.0, which GHSA-6pq8-jhm2-2vcw affects: `qs.stringify` with `arrayFormat: 'comma'` threw an uncaught TypeError on array elements carrying a non-callable own `toString`, a denial of service. qs reaches the web app only through its development tooling and is not part of the deployed site.
