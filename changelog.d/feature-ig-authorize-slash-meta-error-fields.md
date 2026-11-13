### Fixed

- **Connecting Instagram on an iPhone with the Instagram app installed stays in the browser.** The connect link now ends in `/oauth/authorize/`. Instagram's app-link file leaves only that path to the browser and claims Instagram's other web paths for the app, so the link without the slash opened in the app, which cannot finish the login, and nothing came back to storydump.

### Changed

- **A refused Instagram connect or token refresh logs Meta's error code, subcode, type and trace id.** The callback's `instagram connect: exchange refused` line, for any of its three calls (the code exchange, the long-lived exchange, the profile read), now ends with `Meta error code=… subcode=… type=… fbtrace_id=…`, read from the answer's `error` object, or `Meta's error fields could not be read` when the answer holds none. The refresh's `definitively rejected` line carries the same text inside its parentheses, after the HTTP status and before `marked dead`. Nothing else from the answer is logged: never its message, a token, the code or the app secret.
