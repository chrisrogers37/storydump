# GitHub Pages Static Files

This directory was set up as the deployment source for GitHub Pages. It is **not** project documentation.

- **Project documentation**: See [`/documentation/`](../documentation/)
- **Current use**: none in the product. `index.html` is the Instagram story-camera deep-link redirect (PR #116), built but never activated: the approval card's **Open Instagram** button links to `https://www.instagram.com/` directly (`src/services/target/prompts.py`). Activation is tracked at #528; the design is `documentation/archive/instagram-deeplink-redirect.md`. Nothing in the tree references this page, and no workflow deploys it.
