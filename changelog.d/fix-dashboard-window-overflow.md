### Fixed

- **The dashboard no longer lets the browser window scroll past it onto blank page (#10).** On Settings › General the window scrolled far below the dashboard: hidden form elements inside the scrolling content, such as the invite form's role picker, were positioned against the page rather than the content area, and the page grew to reach them. The content area is now their containing block, so only it scrolls.
