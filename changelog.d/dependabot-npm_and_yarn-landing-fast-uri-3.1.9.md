### Security

- **The web app's development dependencies take fast-uri 3.1.9.** It was 3.1.8, whose URI parsing GHSA-w74x-v6f9-j3xg, GHSA-wjr9-j795-jxcr, GHSA-333x-w4r6-6crp and GHSA-xhq7-xmqj-769h affect: a crafted URI could spoof or inject its scheme, confuse its host, or normalize a relative reference to a different path. fast-uri reaches the web app only through its development tooling and is not part of the deployed site.
