### Fixed

- **On phones, the app's Queue and Settings pages moved down 20px as they finished loading (#1581 follow-up).** Their loading placeholder drew the line under the page title as a one-line bar, and on screens up to about 414px wide that line wraps to two, so the content below jumped when the page arrived. The Queue and Settings placeholders now render the page's own header (`components/dashboard/page-headers.tsx`), so the line wraps the same way at every width; the Queue's time zone, unknown until its settings load, is held by an inline bar.
