/**
 * Storydump Onboarding Mini App
 *
 * Telegram WebApp SDK integration for guided setup wizard
 * and returning-user home screen dashboard.
 * No framework — vanilla JS with simple state management.
 */

const App = {
    // State
    chatId: null,
    initData: null,
    setupState: null,
    pollInterval: null,
    pollTimeout: null,
    _currentStep: 'welcome',

    // Mode: 'wizard' (onboarding) or 'home' (returning user dashboard)
    mode: 'wizard',

    // When true, wizard steps show "Save & Return" instead of "Next"/"Skip"
    editingFrom: false,

    // Track which steps were completed vs skipped
    skippedSteps: new Set(),
    folderValidation: null,

    // Schedule config (defaults)
    schedule: {
        postsPerDay: 3,
        postingHoursStart: 14,
        postingHoursEnd: 2,
    },

    /**
     * Initialize the Mini App.
     */
    async init() {
        const tg = window.Telegram && window.Telegram.WebApp;

        // Get URL parameters (chat_id and optional token for browser access)
        const params = new URLSearchParams(window.location.search);
        this.chatId = parseInt(params.get('chat_id'), 10);
        const urlToken = params.get('token');

        if (tg && tg.initData) {
            // Opened as Telegram Mini App — use native auth
            tg.ready();
            tg.expand();
            this._applyTheme(tg.themeParams);
            this.initData = tg.initData;
        } else if (urlToken) {
            // Opened via browser URL with signed token (group chats)
            this.initData = urlToken;
        } else {
            this._showError('Missing authentication data. Please open from Telegram.');
            return;
        }

        if (!this.chatId) {
            this._showError('Missing chat context. Please use /start in your Telegram chat.');
            return;
        }

        // Fetch initial state
        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const response = await this._apiGet('/api/onboarding/init?' + params.toString());
            this.setupState = response.setup_state;

            // Resume from where the user left off
            this._resumeFromState();
        } catch (err) {
            this._showError('Failed to load setup state. Please try again.');
        }
    },

    /**
     * Navigate to a step.
     */
    goToStep(stepName) {
        const stepOrder = ['welcome', 'instagram', 'gdrive', 'media-folder', 'indexing', 'schedule', 'summary'];
        const currentIdx = stepOrder.indexOf(this._currentStep);
        const targetIdx = stepOrder.indexOf(stepName);

        // If jumping forward past intermediate steps, mark them as skipped
        if (targetIdx > currentIdx + 1) {
            for (let i = currentIdx + 1; i < targetIdx; i++) {
                this.skippedSteps.add(stepOrder[i]);
            }
        }

        this._currentStep = stepName;

        // Hide all steps
        document.querySelectorAll('.step').forEach(s => s.classList.add('hidden'));

        // Show target step
        const step = document.getElementById('step-' + stepName);
        if (step) {
            step.classList.remove('hidden');
        }

        // Update status indicators from current state
        if (this.setupState) {
            this._updateStatusIndicators();
        }

        // If going to summary, populate it
        if (stepName === 'summary') {
            this._populateSummary();
        }

        // If going to home, populate dashboard cards
        if (stepName === 'home') {
            this._populateHome();
        }

        // Toggle wizard step navigation based on editing mode
        this._updateStepNavVisibility(stepName);

        // If going to indexing, update the preview from folder validation
        if (stepName === 'indexing' && this.folderValidation) {
            document.getElementById('indexing-file-count').textContent =
                this.folderValidation.file_count;
            document.getElementById('indexing-categories').textContent =
                this.folderValidation.categories.length > 0
                    ? this.folderValidation.categories.join(', ')
                    : 'None';
            document.getElementById('btn-start-indexing').textContent =
                'Index ' + this.folderValidation.file_count + ' Files Now';
        }

        // Stop any active polling when navigating away
        this._stopPolling();
    },

    /**
     * Start OAuth flow for a provider.
     */
    async connectOAuth(provider) {
        const key = provider === 'google-drive' ? 'gdrive' : provider;
        try {
            const queryParams = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const response = await this._apiGet(
                '/api/onboarding/oauth-url/' + provider + '?' + queryParams.toString()
            );

            // Open OAuth URL in new tab
            window.open(response.auth_url, '_blank');

            // Show polling indicator
            document.getElementById(key + '-polling').classList.remove('hidden');
            document.getElementById('btn-connect-' + key).disabled = true;

            // Start polling for OAuth completion
            this._startPolling(key);
        } catch (err) {
            const timeoutEl = document.getElementById(key + '-timeout');
            if (timeoutEl) {
                timeoutEl.textContent = err.message || 'Failed to start connection. Please try again.';
                timeoutEl.classList.remove('hidden');
            }
        }
    },

    /**
     * Validate a Google Drive folder URL.
     */
    async validateFolder() {
        const urlInput = document.getElementById('folder-url');
        const url = urlInput.value.trim();

        if (!url) return;

        document.getElementById('folder-error').classList.add('hidden');
        document.getElementById('folder-result').classList.add('hidden');

        this._showLoading(true);
        try {
            const response = await this._api('/api/onboarding/media-folder', {
                init_data: this.initData,
                chat_id: this.chatId,
                folder_url: url,
            });

            this.folderValidation = response;

            document.getElementById('folder-file-count').textContent = response.file_count;
            document.getElementById('folder-categories').textContent =
                response.categories.length > 0 ? response.categories.join(', ') : 'None';
            document.getElementById('folder-result').classList.remove('hidden');

            // Also pre-populate the indexing step preview
            document.getElementById('indexing-file-count').textContent = response.file_count;
            document.getElementById('indexing-categories').textContent =
                response.categories.length > 0 ? response.categories.join(', ') : 'None';

            // Update local state
            if (this.setupState) {
                this.setupState.media_folder_configured = true;
                this.setupState.media_folder_id = response.folder_id;
            }
        } catch (err) {
            const errorEl = document.getElementById('folder-error');
            errorEl.textContent = err.message || 'Could not access this folder.';
            errorEl.classList.remove('hidden');
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * Trigger media indexing for the configured folder.
     */
    async startIndexing() {
        document.getElementById('indexing-error').classList.add('hidden');
        document.getElementById('indexing-result').classList.add('hidden');
        document.getElementById('indexing-progress').classList.remove('hidden');
        document.getElementById('btn-start-indexing').disabled = true;

        try {
            const response = await this._api('/api/onboarding/start-indexing', {
                init_data: this.initData,
                chat_id: this.chatId,
            });

            // Hide progress, show result
            document.getElementById('indexing-progress').classList.add('hidden');
            document.getElementById('indexing-new-count').textContent = response.new;
            document.getElementById('indexing-total-count').textContent = response.total_processed;

            if (response.errors > 0) {
                document.getElementById('indexing-error-count').textContent = response.errors;
                document.getElementById('indexing-errors').classList.remove('hidden');
            }

            document.getElementById('indexing-result').classList.remove('hidden');

            // Update local state
            if (this.setupState) {
                this.setupState.media_indexed = true;
                this.setupState.media_count = response.new;
            }

            // Auto-advance to schedule step after short delay
            setTimeout(() => this.goToStep('schedule'), 2000);
        } catch (err) {
            document.getElementById('indexing-progress').classList.add('hidden');
            const errorEl = document.getElementById('indexing-error');
            errorEl.textContent = err.message || 'Indexing failed. You can try /sync later.';
            errorEl.classList.remove('hidden');
            document.getElementById('btn-start-indexing').disabled = false;
        }
    },

    /**
     * Select a posts-per-day option.
     */
    selectOption(group, value) {
        // Update button active states
        document.querySelectorAll('#' + group + '-group .btn-option').forEach(btn => {
            btn.classList.toggle('active', parseInt(btn.dataset.value, 10) === value);
        });
        this.schedule.postsPerDay = value;
    },

    /**
     * Select a posting window preset.
     */
    selectWindow(start, end) {
        document.querySelectorAll('#posting-window-group .btn-option').forEach(btn => {
            const btnStart = parseInt(btn.dataset.start, 10);
            const btnEnd = parseInt(btn.dataset.end, 10);
            btn.classList.toggle('active', btnStart === start && btnEnd === end);
        });
        this.schedule.postingHoursStart = start;
        this.schedule.postingHoursEnd = end;
    },

    /**
     * Save schedule settings.
     */
    async saveSchedule() {
        this._showLoading(true);
        try {
            await this._api('/api/onboarding/schedule', {
                init_data: this.initData,
                chat_id: this.chatId,
                posts_per_day: this.schedule.postsPerDay,
                posting_hours_start: this.schedule.postingHoursStart,
                posting_hours_end: this.schedule.postingHoursEnd,
            });
            this.goToStep('summary');
        } catch (err) {
            const errorEl = document.getElementById('schedule-error');
            if (errorEl) {
                errorEl.textContent = err.message || 'Failed to save schedule. Please try again.';
                errorEl.classList.remove('hidden');
            }
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * Finish onboarding and close the Mini App.
     */
    async finishSetup() {
        this._showLoading(true);
        try {
            await this._api('/api/onboarding/complete', {
                init_data: this.initData,
                chat_id: this.chatId,
            });

            // Close the Mini App
            if (window.Telegram && window.Telegram.WebApp) {
                window.Telegram.WebApp.close();
            }
        } catch (err) {
            this._showError('Failed to complete setup. Please try again.');
        } finally {
            this._showLoading(false);
        }
    },

    // ==================== Home Screen Methods ====================

    // Track which cards have been loaded (lazy loading)
    _cardDataLoaded: {},

    /**
     * Populate the home screen dashboard cards from setupState.
     * @param {Object} opts - Options
     * @param {boolean} opts.keepExpanded - If true, don't collapse cards or reset loaded state
     */
    _populateHome(opts) {
        const s = this.setupState || {};
        const keepExpanded = opts && opts.keepExpanded;

        if (!keepExpanded) {
            // Reset card data loaded state on each home populate
            this._cardDataLoaded = {};

            // Collapse all cards
            document.querySelectorAll('.home-card-expandable').forEach(card => {
                card.classList.remove('expanded');
            });
        }

        // Instagram card
        if (s.instagram_connected) {
            this._setHomeBadge('instagram', 'connected', 'Connected');
            this._setHomeDetail('instagram',
                '@' + this._escapeHtml(s.instagram_username || 'unknown'));
        } else {
            this._setHomeBadge('instagram', 'warning', 'Not connected');
            this._setHomeDetail('instagram', 'Tap Edit to connect your account');
        }

        // Google Drive card
        if (s.gdrive_connected && s.gdrive_needs_reconnect) {
            this._setHomeBadge('gdrive', 'error', 'Needs Reconnect');
            this._setHomeDetail('gdrive', 'Token expired \u2014 tap to reconnect');
        } else if (s.gdrive_connected) {
            this._setHomeBadge('gdrive', 'connected', 'Connected');
            this._setHomeDetail('gdrive', this._escapeHtml(s.gdrive_email || 'Connected'));
        } else {
            this._setHomeBadge('gdrive', 'warning', 'Not connected');
            this._setHomeDetail('gdrive', 'Tap to connect Google Drive');
        }

        // Quick Controls card summary
        const deliveryOn = !s.is_paused;
        const dryRunOn = s.dry_run_mode;
        this._setHomeDetail('controls',
            'Delivery: ' + (deliveryOn ? 'ON' : 'OFF') +
            ' \u00B7 Dry Run: ' + (dryRunOn ? 'ON' : 'OFF'));
        // Set toggle states
        const deliveryToggle = document.getElementById('toggle-delivery');
        const dryRunToggle = document.getElementById('toggle-dryrun');
        const igApiToggle = document.getElementById('toggle-instagram-api');
        const verboseToggle = document.getElementById('toggle-verbose');
        const mediaSyncToggle = document.getElementById('toggle-media-sync');
        if (deliveryToggle) deliveryToggle.checked = deliveryOn;
        if (dryRunToggle) dryRunToggle.checked = dryRunOn;
        if (igApiToggle) igApiToggle.checked = !!s.enable_instagram_api;
        if (verboseToggle) verboseToggle.checked = !!s.show_verbose_notifications;
        if (mediaSyncToggle) mediaSyncToggle.checked = !!s.media_sync_enabled;

        // Set numeric setting displays
        this._updateSettingDisplay('posts_per_day', s.posts_per_day || 3);
        this._updateSettingDisplay('posting_hours_start',
            s.posting_hours_start != null ? s.posting_hours_start : 14);
        this._updateSettingDisplay('posting_hours_end',
            s.posting_hours_end != null ? s.posting_hours_end : 2);

        // System Status card summary
        const setupItems = [
            s.instagram_connected,
            s.gdrive_connected,
            s.media_indexed,
            s.posting_active,
            !s.is_paused,
        ];
        const configuredCount = setupItems.filter(Boolean).length;
        if (configuredCount === 5) {
            this._setHomeBadge('status', 'connected', 'All Set');
            this._setHomeDetail('status', '5/5 setup items configured');
        } else {
            this._setHomeBadge('status', 'warning', configuredCount + '/5');
            this._setHomeDetail('status', configuredCount + '/5 setup items configured');
        }

        // Schedule card
        const postsPerDay = s.posts_per_day || 3;
        const start = s.posting_hours_start != null ? s.posting_hours_start : 14;
        const end = s.posting_hours_end != null ? s.posting_hours_end : 2;

        if (s.is_paused) {
            this._setHomeBadge('schedule', 'error', 'Paused');
        } else {
            this._setHomeBadge('schedule', 'connected', 'Active');
        }

        let scheduleDetail = postsPerDay + '/day, ' +
            this._formatHour(start) + '-' + this._formatHour(end) + ' UTC';
        this._setHomeDetail('schedule', scheduleDetail);

        // Queue status card (in-flight items awaiting team action)
        const inFlightCount = s.in_flight_count || 0;
        if (inFlightCount > 0) {
            this._setHomeBadge('queue', 'connected', inFlightCount + ' awaiting review');
        } else {
            this._setHomeBadge('queue', 'neutral', 'Empty');
        }

        let queueDetail = '';
        if (s.last_post_at) {
            const lastDate = new Date(s.last_post_at);
            queueDetail = 'Last post: ' + this._formatRelativeTime(lastDate);
        } else {
            queueDetail = 'No posts yet';
        }
        this._setHomeDetail('queue', queueDetail);

        // Recent Activity card summary
        if (s.last_post_at) {
            const lastDate = new Date(s.last_post_at);
            this._setHomeDetail('history', 'Last post: ' + this._formatRelativeTime(lastDate));
        } else {
            this._setHomeDetail('history', 'No posts yet');
        }

        // Media Library card
        const mediaCount = s.media_count || 0;
        if (mediaCount > 0) {
            this._setHomeBadge('media', 'connected', mediaCount.toLocaleString() + ' files');
        } else {
            this._setHomeBadge('media', 'neutral', 'Empty');
        }
        this._setHomeDetail('media', 'Tap to see categories');
    },

    _setHomeBadge(section, type, text) {
        const el = document.getElementById('home-badge-' + section);
        if (el) {
            el.textContent = text;
            el.className = 'home-card-badge badge-' + type;
        }
    },

    _setHomeDetail(section, html) {
        const el = document.getElementById('home-detail-' + section);
        if (el) {
            el.innerHTML = html;
        }
    },

    /**
     * Toggle a collapsible card open/closed.
     * Lazy-loads data on first expand.
     */
    toggleCard(cardId) {
        const card = document.getElementById('home-card-' + cardId);
        if (!card) return;

        const isExpanded = card.classList.toggle('expanded');

        if (isExpanded && !this._cardDataLoaded[cardId]) {
            this._cardDataLoaded[cardId] = true;
            this._loadCardData(cardId);
        }
    },

    /**
     * Load data for a specific card on first expand.
     */
    async _loadCardData(cardId) {
        const loaders = {
            instagram: () => this._loadAccounts(),
            gdrive: () => this._loadGdriveDetail(),
            status: () => this._loadSystemStatus(),
            queue: () => this._loadQueueDetail('queue'),
            history: () => this._loadHistoryDetail(),
            media: () => this._loadMediaStats(),
            schedule: () => this._loadSchedulePreview(),
        };

        const loader = loaders[cardId];
        if (loader) await loader();
    },

    /**
     * Fetch queue detail and render into queue card.
     */
    async _loadQueueDetail(target) {
        const loadingEl = document.getElementById('queue-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
                limit: 10,
            });
            const data = await this._apiGet('/api/onboarding/queue-detail?' + params.toString());
            this._renderQueueItems(data.items);
        } catch (err) {
            const container = document.getElementById('queue-items-list');
            if (container) {
                container.innerHTML = '<div class="card-body-empty">Failed to load data</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Fetch and render recent posting history.
     */
    async _loadHistoryDetail() {
        const loadingEl = document.getElementById('history-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
                limit: 10,
            });
            const data = await this._apiGet('/api/onboarding/history-detail?' + params.toString());
            this._renderHistoryItems(data.items);
        } catch (err) {
            const container = document.getElementById('history-items-list');
            if (container) {
                container.innerHTML = '<div class="card-body-empty">Failed to load history</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Drop a cached schedule preview after anything that changes the cadence.
     *
     * Cadence, posting window and pause state are all inputs to the slot
     * computation, so a preview rendered before the change is wrong the moment
     * it lands. Reloads in place if the card is open, otherwise lets the next
     * expand fetch it.
     */
    _invalidateSchedulePreview() {
        this._cardDataLoaded['schedule'] = false;
        const card = document.getElementById('home-card-schedule');
        if (card && card.classList.contains('expanded')) {
            this._cardDataLoaded['schedule'] = true;
            this._loadSchedulePreview();
        }
    },

    /**
     * Fetch and render the upcoming schedule preview.
     */
    async _loadSchedulePreview() {
        const loadingEl = document.getElementById('schedule-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
                slots: 5,
            });
            const data = await this._apiGet(
                '/api/onboarding/analytics/schedule-preview?' + params.toString()
            );
            this._renderSchedulePreview(data);
        } catch (err) {
            const container = document.getElementById('schedule-preview-list');
            if (container) {
                container.innerHTML = '<div class="card-body-empty">Failed to load schedule</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Render upcoming slots. Reuses the queue row shape — both answer
     * "what goes out next", so they should read the same.
     */
    _renderSchedulePreview(data) {
        const container = document.getElementById('schedule-preview-list');
        if (!container) return;

        if (data && data.status === 'paused') {
            container.innerHTML =
                '<div class="card-body-empty">Posting is paused \u2014 no slots scheduled</div>';
            return;
        }

        const slots = (data && data.slots) || [];
        if (slots.length === 0) {
            container.innerHTML = '<div class="card-body-empty">No upcoming slots</div>';
            return;
        }

        let html = '';
        for (const slot of slots) {
            const time = new Date(slot.slot_time);
            const category = slot.predicted_category;
            html += '<div class="queue-item-row">' +
                '<div class="item-row-left">' +
                '<div class="item-row-name">' + this._formatShortDateTime(time) + '</div>' +
                '<div class="item-row-meta">' +
                (category ? this._escapeHtml(category) : 'Any category') +
                '</div>' +
                '</div>' +
                '<div class="item-row-right">' +
                '<div class="item-row-time">' + this._formatRelativeTime(time) + '</div>' +
                '</div>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    /**
     * Fetch and render media library stats.
     */
    async _loadMediaStats() {
        const loadingEl = document.getElementById('media-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const data = await this._apiGet('/api/onboarding/media-stats?' + params.toString());
            this._renderCategoryBreakdown(data.categories, data.total_active);
        } catch (err) {
            const container = document.getElementById('media-category-list');
            if (container) {
                container.innerHTML = '<div class="card-body-empty">Failed to load media stats</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Populate Google Drive card body based on setup state.
     */
    _loadGdriveDetail() {
        const s = this.setupState || {};
        const infoEl = document.getElementById('gdrive-connection-info');
        const banner = document.getElementById('gdrive-reconnect-banner');
        const actions = document.getElementById('gdrive-card-actions');
        const reconnectBtn = document.getElementById('btn-gdrive-reconnect');
        const changeFolderBtn = document.getElementById('btn-gdrive-change-folder');
        const disconnectBtn = document.getElementById('btn-gdrive-disconnect');

        if (!infoEl) return;

        if (s.gdrive_connected) {
            let html = '<div class="card-body-info-row">' +
                '<span class="info-label">Account:</span> ' +
                '<span>' + this._escapeHtml(s.gdrive_email || 'Connected') + '</span>' +
                '</div>';
            if (s.media_folder_id) {
                html += '<div class="card-body-info-row">' +
                    '<span class="info-label">Folder:</span> ' +
                    '<span class="info-value-mono">' + this._escapeHtml(s.media_folder_id) + '</span>' +
                    '</div>';
            }
            infoEl.innerHTML = html;

            // Show reconnect banner if token is stale
            if (banner) banner.classList.toggle('hidden', !s.gdrive_needs_reconnect);

            // Show action buttons
            if (reconnectBtn) reconnectBtn.style.display = s.gdrive_needs_reconnect ? '' : 'none';
            if (changeFolderBtn) changeFolderBtn.style.display = s.media_folder_configured ? '' : 'none';
            if (disconnectBtn) disconnectBtn.style.display = '';
        } else {
            infoEl.innerHTML = '<div class="card-body-empty">Not connected</div>';
            if (banner) banner.classList.add('hidden');
            // Show only connect button when disconnected
            if (reconnectBtn) { reconnectBtn.style.display = ''; reconnectBtn.textContent = 'Connect'; }
            if (changeFolderBtn) changeFolderBtn.style.display = 'none';
            if (disconnectBtn) disconnectBtn.style.display = 'none';
        }

        if (actions) actions.style.display = '';
    },

    /**
     * Show confirmation dialog for disconnecting Google Drive.
     */
    confirmDisconnectGdrive() {
        const confirm = document.getElementById('gdrive-disconnect-confirm');
        const actions = document.getElementById('gdrive-card-actions');
        if (confirm) confirm.classList.remove('hidden');
        if (actions) actions.style.display = 'none';
    },

    /**
     * Cancel Google Drive disconnect.
     */
    cancelDisconnectGdrive() {
        const confirm = document.getElementById('gdrive-disconnect-confirm');
        const actions = document.getElementById('gdrive-card-actions');
        if (confirm) confirm.classList.add('hidden');
        if (actions) actions.style.display = '';
    },

    /**
     * Execute Google Drive disconnect after confirmation.
     */
    async executeDisconnectGdrive() {
        this.cancelDisconnectGdrive();
        this._showLoading(true);
        try {
            await this._api('/api/onboarding/disconnect-gdrive', {
                init_data: this.initData,
                chat_id: this.chatId,
            });
            await this._refreshHome({ keepExpanded: true });
        } catch (err) {
            this._showCardError('gdrive-connection-info', err.message || 'Failed to disconnect');
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * Fetch and render system status (setup checklist + health checks).
     */
    async _loadSystemStatus() {
        const loadingEl = document.getElementById('status-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        // Render setup checklist from local state (no API call needed)
        this._renderSetupChecklist();

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const data = await this._apiGet('/api/onboarding/system-status?' + params.toString());
            this._renderHealthChecks(data.checks);

            // Update badge based on health check results
            if (data.status === 'healthy') {
                this._setHomeBadge('status', 'connected', 'Healthy');
            } else {
                const unhealthyCount = Object.values(data.checks)
                    .filter(c => !c.healthy).length;
                this._setHomeBadge('status', 'error', unhealthyCount + ' Issue' + (unhealthyCount !== 1 ? 's' : ''));
            }
        } catch (err) {
            const container = document.getElementById('status-health-checks');
            if (container) {
                // A 403 here is the admin gate doing its job (#898), not a
                // fault. Rendering it as "Failed to load" made a deliberate
                // authorisation decision look like a malfunction — which sends
                // support chasing a defect that does not exist, and teaches
                // users to ignore this line, so a REAL failure of the same card
                // would hide behind it. The setup checklist above is unaffected;
                // it renders from local state and needs no admin rights.
                const message = this._failureMessage(err, {
                    refused: 'Deployment health is available to administrators only.',
                    failed: 'Failed to load health data',
                });
                container.innerHTML =
                    '<div class="card-body-empty">' + this._escapeHtml(message) + '</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Render setup checklist from setupState.
     */
    _renderSetupChecklist() {
        const s = this.setupState || {};
        const container = document.getElementById('status-checklist');
        if (!container) return;

        const items = [
            {
                name: 'Instagram',
                ok: s.instagram_connected,
                detail: s.instagram_username ? '@' + this._escapeHtml(s.instagram_username) : 'Not connected',
            },
            {
                name: 'Google Drive',
                ok: s.gdrive_connected,
                detail: s.gdrive_email ? this._escapeHtml(s.gdrive_email) : 'Not connected',
            },
            {
                name: 'Media Library',
                ok: s.media_indexed,
                detail: s.media_count ? s.media_count + ' files' : 'Not indexed',
            },
            {
                name: 'Posting',
                ok: s.posting_active,
                detail: s.posting_active
                    ? (s.posts_per_day || 3) + '/day active'
                    : 'No recent posts',
            },
            {
                name: 'Delivery',
                ok: !s.is_paused,
                detail: s.is_paused ? 'Paused' : (s.dry_run_mode ? 'Dry Run' : 'Live'),
            },
        ];

        let html = '<div class="status-section-label">Setup</div>';
        for (const item of items) {
            const icon = item.ok ? '&#x2705;' : '&#x26A0;&#xFE0F;';
            html += '<div class="status-check-row">' +
                '<span class="status-check-icon">' + icon + '</span>' +
                '<span class="status-check-name">' + this._escapeHtml(item.name) + '</span>' +
                '<span class="status-check-detail">' + item.detail + '</span>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    /**
     * Render health check results from API.
     */
    _renderHealthChecks(checks) {
        const container = document.getElementById('status-health-checks');
        if (!container) return;

        const displayOrder = [
            { key: 'database', label: 'Database' },
            { key: 'telegram', label: 'Telegram' },
            { key: 'instagram_api', label: 'Instagram API' },
            { key: 'queue', label: 'Queue' },
            { key: 'recent_posts', label: 'Recent Posts' },
            { key: 'media_sync', label: 'Media Sync' },
        ];

        let html = '<div class="status-section-label">Health</div>';
        for (const { key, label } of displayOrder) {
            const check = checks[key];
            if (!check) continue;

            const icon = check.healthy ? '&#x2705;' : '&#x274C;';
            html += '<div class="status-check-row">' +
                '<span class="status-check-icon">' + icon + '</span>' +
                '<span class="status-check-name">' + this._escapeHtml(label) + '</span>' +
                '<span class="status-check-detail">' + this._escapeHtml(check.message || '') + '</span>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    // ==================== Account Management ====================

    // Track account pending removal (for confirmation dialog)
    _pendingRemoveAccountId: null,

    /**
     * Fetch and render Instagram accounts.
     */
    async _loadAccounts() {
        const loadingEl = document.getElementById('instagram-account-loading');
        if (loadingEl) loadingEl.classList.remove('hidden');

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const data = await this._apiGet('/api/onboarding/accounts?' + params.toString());
            this._renderAccounts(data.accounts);
        } catch (err) {
            const container = document.getElementById('instagram-account-list');
            if (container) {
                container.innerHTML = '<div class="card-body-empty">Failed to load accounts</div>';
            }
        } finally {
            if (loadingEl) loadingEl.classList.add('hidden');
        }
    },

    /**
     * Render account list into the Instagram card body.
     */
    _renderAccounts(accounts) {
        const container = document.getElementById('instagram-account-list');
        if (!container) return;

        if (!accounts || accounts.length === 0) {
            container.innerHTML = '<div class="card-body-empty">No accounts connected</div>';
            return;
        }

        let html = '';
        for (const acct of accounts) {
            html += '<div class="account-row">' +
                '<div class="account-info">' +
                '<div class="account-name">' + this._escapeHtml(acct.display_name) + '</div>' +
                '<div class="account-username">@' + this._escapeHtml(acct.instagram_username || '') + '</div>' +
                '</div>' +
                '<div class="account-actions">';

            if (acct.is_active) {
                html += '<span class="account-active-badge">Active</span>';
            } else {
                html += '<button class="btn-account-action btn-account-switch" ' +
                    'data-action="switchAccount" data-args=\'' +
                    this._escapeAttr(JSON.stringify([acct.id])) + '\'>Switch</button>';
            }
            html += '<button class="btn-account-action btn-account-remove" ' +
                'data-action="confirmRemoveAccount" data-args=\'' +
                this._escapeAttr(JSON.stringify([acct.id])) + '\'>Remove</button>';
            html += '</div></div>';
        }

        container.innerHTML = html;
    },

    /**
     * Switch active Instagram account.
     */
    async switchAccount(accountId) {
        this._showLoading(true);
        try {
            const data = await this._api('/api/onboarding/switch-account', {
                init_data: this.initData,
                chat_id: this.chatId,
                account_id: accountId,
            });

            // Update local state
            if (this.setupState) {
                this.setupState.instagram_connected = true;
                this.setupState.instagram_username = data.instagram_username;
            }

            // Update card summary
            this._setHomeBadge('instagram', 'connected', 'Connected');
            this._setHomeDetail('instagram',
                '@' + this._escapeHtml(data.instagram_username || 'unknown'));

            // Reload account list to reflect new active state
            this._cardDataLoaded['instagram'] = false;
            await this._loadAccounts();
        } catch (err) {
            this._showCardError('instagram-account-list', err.message || 'Failed to switch account');
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * Show confirmation dialog for removing an account.
     */
    confirmRemoveAccount(accountId) {
        this._pendingRemoveAccountId = accountId;
        const confirm = document.getElementById('remove-account-confirm');
        const actions = document.getElementById('instagram-account-actions');
        if (confirm) confirm.classList.remove('hidden');
        if (actions) actions.classList.add('hidden');
    },

    /**
     * Cancel account removal.
     */
    cancelRemoveAccount() {
        this._pendingRemoveAccountId = null;
        const confirm = document.getElementById('remove-account-confirm');
        const actions = document.getElementById('instagram-account-actions');
        if (confirm) confirm.classList.add('hidden');
        if (actions) actions.classList.remove('hidden');
    },

    /**
     * Execute account removal after confirmation.
     */
    async executeRemoveAccount() {
        const accountId = this._pendingRemoveAccountId;
        if (!accountId) return;

        this.cancelRemoveAccount();
        this._showLoading(true);
        try {
            await this._api('/api/onboarding/remove-account', {
                init_data: this.initData,
                chat_id: this.chatId,
                account_id: accountId,
            });

            // Refresh home to get updated state
            await this._refreshHome({ keepExpanded: true });
        } catch (err) {
            this._showCardError('instagram-account-list', err.message || 'Failed to remove account');
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * Show the add account form (manual token entry).
     */
    showAddAccountForm() {
        const form = document.getElementById('add-account-form');
        const actions = document.getElementById('instagram-account-actions');
        const error = document.getElementById('add-account-error');
        const success = document.getElementById('add-account-success');
        if (form) form.classList.remove('hidden');
        if (actions) actions.classList.add('hidden');
        if (error) error.classList.add('hidden');
        if (success) success.classList.add('hidden');
    },

    /**
     * Cancel and hide the add account form.
     */
    cancelAddAccount() {
        const form = document.getElementById('add-account-form');
        const actions = document.getElementById('instagram-account-actions');
        if (form) form.classList.add('hidden');
        if (actions) actions.classList.remove('hidden');
        // Clear inputs
        const name = document.getElementById('add-account-name');
        const id = document.getElementById('add-account-id');
        const token = document.getElementById('add-account-token');
        if (name) name.value = '';
        if (id) id.value = '';
        if (token) token.value = '';
        const error = document.getElementById('add-account-error');
        const success = document.getElementById('add-account-success');
        if (error) error.classList.add('hidden');
        if (success) success.classList.add('hidden');
    },

    /**
     * Submit the add account form.
     */
    async submitAddAccount() {
        const nameEl = document.getElementById('add-account-name');
        const idEl = document.getElementById('add-account-id');
        const tokenEl = document.getElementById('add-account-token');
        const errorEl = document.getElementById('add-account-error');
        const successEl = document.getElementById('add-account-success');
        const submitBtn = document.getElementById('btn-submit-account');

        const displayName = (nameEl?.value || '').trim();
        const accountId = (idEl?.value || '').trim();
        const accessToken = (tokenEl?.value || '').trim();

        // Client-side validation
        if (!displayName) {
            this._showAddAccountError('Please enter a display name.');
            return;
        }
        if (!accountId || !/^\d+$/.test(accountId)) {
            this._showAddAccountError('Account ID must be a numeric value.');
            return;
        }
        if (!accessToken) {
            this._showAddAccountError('Please paste your access token.');
            return;
        }

        // Hide error, show loading
        if (errorEl) errorEl.classList.add('hidden');
        if (successEl) successEl.classList.add('hidden');
        if (submitBtn) {
            submitBtn.disabled = true;
            submitBtn.textContent = 'Verifying...';
        }

        try {
            const data = await this._api('/api/onboarding/add-account', {
                init_data: this.initData,
                chat_id: this.chatId,
                display_name: displayName,
                instagram_account_id: accountId,
                access_token: accessToken,
            });

            // Clear token from input immediately
            if (tokenEl) tokenEl.value = '';

            // Show success
            if (successEl) {
                const action = data.is_update ? 'updated' : 'added';
                successEl.textContent = `Account @${data.instagram_username} ${action} successfully.`;
                successEl.classList.remove('hidden');
            }

            // Refresh account list after brief delay
            setTimeout(async () => {
                this.cancelAddAccount();
                await this._refreshHome({ keepExpanded: true });
            }, 1500);

        } catch (err) {
            this._showAddAccountError(err.message || 'Failed to add account.');
        } finally {
            if (submitBtn) {
                submitBtn.disabled = false;
                submitBtn.textContent = 'Add Account';
            }
        }
    },

    /**
     * Show an inline error in the add account form.
     */
    _showAddAccountError(message) {
        const errorEl = document.getElementById('add-account-error');
        if (errorEl) {
            errorEl.textContent = message;
            errorEl.classList.remove('hidden');
        }
    },

    // Toggle element IDs mapped to setting names
    _toggleIds: {
        'is_paused': 'toggle-delivery',
        'dry_run_mode': 'toggle-dryrun',
        'enable_instagram_api': 'toggle-instagram-api',
        'show_verbose_notifications': 'toggle-verbose',
        'media_sync_enabled': 'toggle-media-sync',
    },

    /**
     * Toggle a boolean setting via API.
     */
    async toggleSetting(settingName) {
        try {
            const data = await this._api('/api/onboarding/toggle-setting', {
                init_data: this.initData,
                chat_id: this.chatId,
                setting_name: settingName,
            });

            // Update local state
            if (this.setupState) {
                this.setupState[settingName] = data.new_value;
            }

            // Update summary text
            this._updateControlsSummary();

            // Update schedule badge
            if (settingName === 'is_paused') {
                if (this.setupState.is_paused) {
                    this._setHomeBadge('schedule', 'error', 'Paused');
                } else {
                    this._setHomeBadge('schedule', 'connected', 'Active');
                }
                this._invalidateSchedulePreview();
            }
        } catch (err) {
            // Revert toggle on failure
            const toggleId = this._toggleIds[settingName];
            if (toggleId) {
                const toggle = document.getElementById(toggleId);
                if (toggle) toggle.checked = !toggle.checked;
            }
        }
    },

    /**
     * Adjust a numeric setting by a delta (stepper +/- buttons).
     */
    async adjustSetting(settingName, delta) {
        const s = this.setupState || {};
        const current = s[settingName] || 0;
        let newValue = current + delta;

        // Clamp values
        if (settingName === 'posts_per_day') {
            newValue = Math.max(1, Math.min(50, newValue));
        } else {
            // Hours: wrap around 0-23
            newValue = ((newValue % 24) + 24) % 24;
        }

        if (newValue === current) return;

        // Optimistic UI update
        this.setupState[settingName] = newValue;
        this._updateSettingDisplay(settingName, newValue);

        try {
            await this._api('/api/onboarding/update-setting', {
                init_data: this.initData,
                chat_id: this.chatId,
                setting_name: settingName,
                value: newValue,
            });

            // Update controls summary
            this._updateControlsSummary();
            this._invalidateSchedulePreview();
        } catch (err) {
            // Revert on failure
            this.setupState[settingName] = current;
            this._updateSettingDisplay(settingName, current);
        }
    },

    /**
     * Update the displayed value for a numeric setting.
     */
    _updateSettingDisplay(settingName, value) {
        const displayMap = {
            'posts_per_day': { id: 'setting-posts-per-day', fmt: v => String(v) },
            'posting_hours_start': { id: 'setting-posting-hours-start', fmt: v => this._formatHour(v) },
            'posting_hours_end': { id: 'setting-posting-hours-end', fmt: v => this._formatHour(v) },
        };
        const mapping = displayMap[settingName];
        if (mapping) {
            const el = document.getElementById(mapping.id);
            if (el) el.textContent = mapping.fmt(value);
        }
    },

    /**
     * Update the Quick Controls card summary text.
     */
    _updateControlsSummary() {
        const s = this.setupState || {};
        const deliveryOn = !s.is_paused;
        const dryRunOn = s.dry_run_mode;
        this._setHomeDetail('controls',
            'Delivery: ' + (deliveryOn ? 'ON' : 'OFF') +
            ' \u00B7 Dry Run: ' + (dryRunOn ? 'ON' : 'OFF'));
    },

    /**
     * Trigger media sync from the dashboard.
     */
    async syncMedia() {
        const btn = document.getElementById('btn-sync-media');
        const resultEl = document.getElementById('sync-result');
        if (btn) btn.disabled = true;
        if (resultEl) {
            resultEl.classList.add('hidden');
            resultEl.textContent = '';
        }

        this._showLoading(true);
        try {
            const data = await this._api('/api/onboarding/sync-media', {
                init_data: this.initData,
                chat_id: this.chatId,
            });

            // Build result message
            const parts = [];
            if (data.new > 0) parts.push(data.new + ' new');
            if (data.updated > 0) parts.push(data.updated + ' updated');
            if (data.deactivated > 0) parts.push(data.deactivated + ' removed');
            if (data.errors > 0) parts.push(data.errors + ' errors');

            const msg = parts.length > 0
                ? 'Synced: ' + parts.join(', ')
                : 'No changes found';

            if (resultEl) {
                resultEl.textContent = msg;
                resultEl.className = 'sync-result' + (data.errors > 0 ? ' sync-result-warning' : '');
            }

            // Update media count in local state
            if (this.setupState && data.new > 0) {
                this.setupState.media_count = (this.setupState.media_count || 0) + data.new;
                this.setupState.media_indexed = true;
                const mediaCount = this.setupState.media_count;
                this._setHomeBadge('media', 'connected', mediaCount.toLocaleString() + ' files');
            }
        } catch (err) {
            if (resultEl) {
                resultEl.textContent = err.message || 'Sync failed';
                resultEl.className = 'sync-result sync-result-warning';
            }
        } finally {
            this._showLoading(false);
            if (btn) btn.disabled = false;
        }
    },

    /**
     * Re-fetch setup state to refresh dashboard numbers.
     * @param {Object} opts - Options passed to _populateHome
     * @param {boolean} opts.keepExpanded - If true, don't collapse cards
     */
    async _refreshHome(opts) {
        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const response = await this._apiGet('/api/onboarding/init?' + params.toString());
            this.setupState = response.setup_state;
            this._populateHome(opts);

            // If keeping cards expanded, reload data for any currently expanded cards
            if (opts && opts.keepExpanded) {
                const expandedCards = document.querySelectorAll('.home-card-expandable.expanded');
                for (const card of expandedCards) {
                    const cardId = card.id.replace('home-card-', '');
                    this._cardDataLoaded[cardId] = false;
                    await this._loadCardData(cardId);
                }
            }
        } catch (err) {
            // Non-critical
        }
    },

    // ==================== Render Helpers ====================

    _renderQueueItems(items) {
        const container = document.getElementById('queue-items-list');
        if (!container) return;

        if (!items || items.length === 0) {
            container.innerHTML = '<div class="card-body-empty">Queue is empty</div>';
            return;
        }

        let html = '';
        for (const item of items) {
            const time = new Date(item.scheduled_for);
            html += '<div class="queue-item-row">' +
                '<div class="item-row-left">' +
                '<div class="item-row-name">' + this._escapeHtml(item.media_name) + '</div>' +
                '<div class="item-row-meta">' + this._escapeHtml(item.category) + '</div>' +
                '</div>' +
                '<div class="item-row-right">' +
                '<div class="item-row-time">' + this._formatRelativeTime(time) + '</div>' +
                '</div>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    _renderHistoryItems(items) {
        const container = document.getElementById('history-items-list');
        if (!container) return;

        if (!items || items.length === 0) {
            container.innerHTML = '<div class="card-body-empty">No posting history</div>';
            return;
        }

        let html = '';
        for (const item of items) {
            const time = new Date(item.posted_at);
            const statusClass = 'status-' + (item.status || 'posted');
            html += '<div class="history-item-row">' +
                '<div class="item-row-left">' +
                '<div class="item-row-name">' + this._escapeHtml(item.media_name) + '</div>' +
                '<div class="item-row-meta">' + this._escapeHtml(item.category) +
                ' \u00B7 ' + this._escapeHtml(item.posting_method === 'instagram_api' ? 'API' : 'Manual') +
                '</div>' +
                '</div>' +
                '<div class="item-row-right">' +
                '<span class="item-row-status ' + statusClass + '">' +
                this._escapeHtml(item.status || 'posted') + '</span>' +
                '<div class="item-row-time">' + this._formatRelativeTime(time) + '</div>' +
                '</div>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    _renderCategoryBreakdown(categories, totalActive) {
        const container = document.getElementById('media-category-list');
        if (!container) return;

        if (!categories || categories.length === 0) {
            container.innerHTML = '<div class="card-body-empty">No media indexed</div>';
            return;
        }

        const maxCount = categories[0].count;
        let html = '';
        for (const cat of categories) {
            const pct = maxCount > 0 ? Math.round((cat.count / maxCount) * 100) : 0;
            html += '<div class="category-row">' +
                '<span class="category-name">' + this._escapeHtml(cat.name) + '</span>' +
                '<div class="category-bar-wrap">' +
                '<div class="category-bar" style="width:' + pct + '%"></div>' +
                '</div>' +
                '<span class="category-count">' + cat.count + '</span>' +
                '</div>';
        }

        container.innerHTML = html;
    },

    /**
     * Enter edit mode for a section — jumps to the wizard step
     * with "Save & Return" shown instead of "Next"/"Skip".
     */
    editSection(section) {
        this.editingFrom = true;
        this.mode = 'wizard';
        this.goToStep(section);
    },

    /**
     * Return from edit mode to the home screen.
     * Re-fetches state to get latest data, then shows home.
     */
    async returnToHome() {
        this.editingFrom = false;
        this.mode = 'home';

        try {
            const params = new URLSearchParams({
                init_data: this.initData,
                chat_id: this.chatId,
            });
            const response = await this._apiGet('/api/onboarding/init?' + params.toString());
            this.setupState = response.setup_state;
        } catch (err) {
            // Non-critical: show home with stale data
        }

        this.goToStep('home');
    },

    /**
     * Save schedule settings then return to home (edit mode).
     */
    async saveScheduleAndReturn() {
        this._showLoading(true);
        try {
            await this._api('/api/onboarding/schedule', {
                init_data: this.initData,
                chat_id: this.chatId,
                posts_per_day: this.schedule.postsPerDay,
                posting_hours_start: this.schedule.postingHoursStart,
                posting_hours_end: this.schedule.postingHoursEnd,
            });
            await this.returnToHome();
        } catch (err) {
            const errorEl = document.getElementById('schedule-error');
            if (errorEl) {
                errorEl.textContent = err.message || 'Failed to save schedule. Please try again.';
                errorEl.classList.remove('hidden');
            }
        } finally {
            this._showLoading(false);
        }
    },

    /**
     * "Run Full Setup Again" — reset to wizard mode from step 1.
     */
    runFullSetup() {
        this.editingFrom = false;
        this.mode = 'wizard';
        this.goToStep('welcome');
    },

    /**
     * Show or hide "Save & Return" vs normal navigation
     * based on whether we are editing from home.
     */
    _updateStepNavVisibility(stepName) {
        const editableSteps = ['instagram', 'gdrive', 'media-folder', 'schedule'];

        editableSteps.forEach(step => {
            const returnNav = document.getElementById('return-nav-' + step);
            if (returnNav) {
                returnNav.classList.toggle('hidden', !this.editingFrom);
            }
        });

        // For steps with regular nav, hide it when editing
        if (this.editingFrom) {
            document.querySelectorAll('.step-nav').forEach(el => {
                el.classList.add('hidden');
            });
        } else {
            document.querySelectorAll('.step-nav').forEach(el => {
                el.classList.remove('hidden');
            });
        }

        // Toggle schedule buttons
        const scheduleNext = document.getElementById('btn-schedule-next');
        const scheduleReturn = document.getElementById('btn-schedule-return');
        if (scheduleNext && scheduleReturn) {
            scheduleNext.classList.toggle('hidden', this.editingFrom);
            scheduleReturn.classList.toggle('hidden', !this.editingFrom);
        }
    },

    // --- Private methods ---

    /**
     * Determine which step to show based on current state.
     */
    _resumeFromState() {
        if (!this.setupState) {
            this.mode = 'wizard';
            this.goToStep('welcome');
            return;
        }

        if (this.setupState.onboarding_completed) {
            // Returning user — show home screen dashboard
            this.mode = 'home';
            this.goToStep('home');
            return;
        }

        // Onboarding in progress — show wizard
        this.mode = 'wizard';

        // Update schedule defaults from saved state
        this.schedule.postsPerDay = this.setupState.posts_per_day || 3;
        this.schedule.postingHoursStart = this.setupState.posting_hours_start || 14;
        this.schedule.postingHoursEnd = this.setupState.posting_hours_end || 2;

        // Resume from saved step if available
        const step = this.setupState.onboarding_step;
        if (step && document.getElementById('step-' + step)) {
            this.goToStep(step);
            return;
        }

        // Default: start from the beginning
        this.goToStep('welcome');
    },

    /**
     * Update connection status indicators.
     */
    _updateStatusIndicators() {
        const s = this.setupState;

        // Instagram
        const igStatus = document.getElementById('instagram-status');
        if (igStatus && s.instagram_connected) {
            igStatus.innerHTML =
                '<div class="status-icon status-connected">&#9679;</div>' +
                '<span>Connected: @' + this._escapeHtml(s.instagram_username || '') + '</span>';
            document.getElementById('btn-connect-instagram').textContent = 'Connected';
            document.getElementById('btn-connect-instagram').disabled = true;
            const reconnectIg = document.getElementById('btn-reconnect-instagram');
            if (reconnectIg) reconnectIg.classList.remove('hidden');
        }

        // Google Drive
        const gdStatus = document.getElementById('gdrive-status');
        if (gdStatus && s.gdrive_connected) {
            gdStatus.innerHTML =
                '<div class="status-icon status-connected">&#9679;</div>' +
                '<span>Connected: ' + this._escapeHtml(s.gdrive_email || '') + '</span>';
            document.getElementById('btn-connect-gdrive').textContent = 'Connected';
            document.getElementById('btn-connect-gdrive').disabled = true;
            const reconnectGd = document.getElementById('btn-reconnect-gdrive');
            if (reconnectGd) reconnectGd.classList.remove('hidden');
        }

        // Media folder
        if (s.media_folder_configured) {
            const folderUrlInput = document.getElementById('folder-url');
            if (folderUrlInput && s.media_folder_id) {
                folderUrlInput.value = 'https://drive.google.com/drive/folders/' + s.media_folder_id;
            }
        }
    },

    /**
     * Populate the summary step.
     */
    _populateSummary() {
        const s = this.setupState || {};

        document.getElementById('summary-instagram').textContent =
            s.instagram_connected ? '@' + (s.instagram_username || 'connected') : 'Skipped';

        document.getElementById('summary-gdrive').textContent =
            s.gdrive_connected ? s.gdrive_email || 'Connected' : 'Skipped';

        document.getElementById('summary-media-folder').textContent =
            s.media_folder_configured ? 'Configured' : 'Skipped';

        document.getElementById('summary-media-indexed').textContent =
            s.media_indexed ? s.media_count + ' files' : 'Skipped';

        document.getElementById('summary-schedule').textContent =
            this.schedule.postsPerDay + ' posts/day';

        document.getElementById('summary-window').textContent =
            this._formatHour(this.schedule.postingHoursStart) + ' - ' +
            this._formatHour(this.schedule.postingHoursEnd) + ' UTC';
    },

    /**
     * Start polling for OAuth completion.
     */
    _startPolling(provider) {
        this._stopPolling();

        this.pollInterval = setInterval(async () => {
            try {
                const params = new URLSearchParams({
                    init_data: this.initData,
                    chat_id: this.chatId,
                });
                const response = await this._apiGet('/api/onboarding/init?' + params.toString());
                this.setupState = response.setup_state;

                const connected = provider === 'instagram'
                    ? this.setupState.instagram_connected
                    : this.setupState.gdrive_connected;

                if (connected) {
                    this._stopPolling();
                    this._updateStatusIndicators();

                    // Auto-advance depends on mode
                    if (this.mode === 'home') {
                        // Added account from dashboard — refresh home
                        setTimeout(() => this._refreshHome({ keepExpanded: true }), 800);
                    } else if (this.editingFrom) {
                        setTimeout(() => this.returnToHome(), 800);
                    } else if (provider === 'instagram') {
                        setTimeout(() => this.goToStep('gdrive'), 800);
                    } else {
                        setTimeout(() => this.goToStep('media-folder'), 800);
                    }
                }
            } catch (_) {
                // Silently retry on poll failure
            }
        }, 3000);

        // Stop after 10 minutes with user feedback
        this.pollTimeout = setTimeout(() => {
            this._stopPolling();
            this._showPollingTimeout(provider);
        }, 600000);
    },

    /**
     * Show timeout message and re-enable the connect button after polling expires.
     */
    _showPollingTimeout(provider) {
        const key = provider === 'google-drive' ? 'gdrive' : provider;
        const btn = document.getElementById('btn-connect-' + key);
        if (btn) {
            btn.disabled = false;
            btn.textContent = 'Retry Connection';
        }
        const timeoutEl = document.getElementById(key + '-timeout');
        if (timeoutEl) timeoutEl.classList.remove('hidden');
    },

    _stopPolling() {
        if (this.pollInterval) {
            clearInterval(this.pollInterval);
            this.pollInterval = null;
        }
        if (this.pollTimeout) {
            clearTimeout(this.pollTimeout);
            this.pollTimeout = null;
        }
        // Hide all polling indicators
        document.querySelectorAll('.polling-indicator').forEach(el => el.classList.add('hidden'));
    },

    /**
     * POST to an API endpoint.
     */
    async _api(path, body) {
        const response = await fetch(path, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            // slowapi's 429 body is {"error": ...}, not {"detail": ...}
            const err = new Error(error.detail || error.error || 'Request failed');
            // Callers need to distinguish a refusal from a fault. Matching on
            // the detail string would couple the UI to server copy; the status
            // is the stable signal. See #922.
            err.status = response.status;
            throw err;
        }

        return response.json();
    },

    /**
     * GET from an API endpoint.
     */
    async _apiGet(path) {
        const response = await fetch(path);

        if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            // slowapi's 429 body is {"error": ...}, not {"detail": ...}
            const err = new Error(error.detail || error.error || 'Request failed');
            // Callers need to distinguish a refusal from a fault. Matching on
            // the detail string would couple the UI to server copy; the status
            // is the stable signal. See #922.
            err.status = response.status;
            throw err;
        }

        return response.json();
    },

    /**
     * Human message for a failed request, enumerated by HTTP status.
     *
     * The point is that these are DIFFERENT OUTCOMES, not shades of one
     * failure — collapsing any two destroys information the user needs, which
     * is what #922 and #928 were both about. 401 and 429 are actionable and
     * mean the same thing on every card; the 403 refusal differs per card, so
     * the caller supplies it.
     *
     * Anything else falls to `failed` deliberately: a 5xx, a 422 from a
     * malformed URL, or a network error carrying no status at all are alike in
     * the only way that matters here — the user cannot respond to them
     * differently. That residual is the honest remainder of an enumeration,
     * not a catch-all hiding named cases.
     */
    _failureMessage(err, { refused, failed }) {
        switch (err && err.status) {
            case 401:
                return 'Your session has expired. Reopen this page from Telegram.';
            case 403:
                return refused;
            case 429:
                return 'Too many requests. Try again in a moment.';
            default:
                return failed;
        }
    },

    /**
     * Apply Telegram theme colors.
     */
    _applyTheme(params) {
        if (!params) return;
        const root = document.documentElement;
        if (params.bg_color) root.style.setProperty('--tg-theme-bg-color', params.bg_color);
        if (params.text_color) root.style.setProperty('--tg-theme-text-color', params.text_color);
        if (params.hint_color) root.style.setProperty('--tg-theme-hint-color', params.hint_color);
        if (params.link_color) root.style.setProperty('--tg-theme-link-color', params.link_color);
        if (params.button_color) root.style.setProperty('--tg-theme-button-color', params.button_color);
        if (params.button_text_color) root.style.setProperty('--tg-theme-button-text-color', params.button_text_color);
        if (params.secondary_bg_color) root.style.setProperty('--tg-theme-secondary-bg-color', params.secondary_bg_color);

        document.body.style.backgroundColor = params.bg_color || '#ffffff';
    },

    _showLoading(show) {
        document.getElementById('loading-overlay').classList.toggle('hidden', !show);
    },

    _showError(message) {
        // For critical errors, replace the whole app content with recovery option
        const app = document.getElementById('app');
        app.innerHTML =
            '<div class="step"><div class="step-content" style="text-align:center;padding-top:60px">' +
            '<h2>Oops</h2><p class="subtitle">' + this._escapeHtml(message) + '</p>' +
            '<button class="btn btn-primary" data-action="reloadPage" style="margin-top:24px">Reload</button>' +
            '</div></div>';
    },

    _escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    },

    /**
     * Escape for an HTML *attribute* value.
     *
     * _escapeHtml is textContent-based, so it encodes & < > and leaves quotes
     * alone — correct in text context, no defence inside a quoted attribute.
     * Anything interpolated into an attribute goes through this instead.
     */
    _escapeAttr(text) {
        return String(text)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    },

    _showCardError(containerId, message) {
        const el = document.getElementById(containerId);
        if (!el) return;
        const existing = el.querySelector('.card-body-error');
        if (existing) existing.remove();
        el.insertAdjacentHTML('beforeend',
            '<div class="card-body-error">' + this._escapeHtml(message) + '</div>');
    },

    _formatHour(h) {
        if (h === 0) return '12am';
        if (h === 12) return '12pm';
        if (h < 12) return h + 'am';
        return (h - 12) + 'pm';
    },

    _formatRelativeTime(date) {
        const now = new Date();
        const diffMs = now - date;
        const absDiffMs = Math.abs(diffMs);
        const isFuture = diffMs < 0;

        const minutes = Math.floor(absDiffMs / (1000 * 60));
        const hours = Math.floor(absDiffMs / (1000 * 60 * 60));
        const days = Math.floor(absDiffMs / (1000 * 60 * 60 * 24));

        if (minutes < 1) return isFuture ? 'now' : 'just now';
        if (minutes < 60) return isFuture ? 'in ' + minutes + 'm' : minutes + 'm ago';
        if (hours < 24) return isFuture ? 'in ' + hours + 'h' : hours + 'h ago';
        if (days < 7) return isFuture ? 'in ' + days + 'd' : days + 'd ago';

        return this._formatShortDate(date);
    },

    _formatShortDate(date) {
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
            'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        return months[date.getMonth()] + ' ' + date.getDate();
    },

    /**
     * Day + clock time for a scheduled slot, in UTC.
     *
     * UTC rather than local because the posting window is configured in UTC
     * and the Schedule card's own summary line states it — rendering slots in
     * the viewer's zone would contradict the line directly above them.
     */
    _formatShortDateTime(date) {
        const months = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
            'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
        const now = new Date();
        const isSameUTCDay = (a, b) =>
            a.getUTCFullYear() === b.getUTCFullYear() &&
            a.getUTCMonth() === b.getUTCMonth() &&
            a.getUTCDate() === b.getUTCDate();

        const tomorrow = new Date(now.getTime() + 24 * 60 * 60 * 1000);
        let day;
        if (isSameUTCDay(date, now)) {
            day = 'Today';
        } else if (isSameUTCDay(date, tomorrow)) {
            day = 'Tomorrow';
        } else {
            day = months[date.getUTCMonth()] + ' ' + date.getUTCDate();
        }

        const h = date.getUTCHours();
        const m = String(date.getUTCMinutes()).padStart(2, '0');
        const suffix = h < 12 ? 'am' : 'pm';
        let hour12 = h % 12;
        if (hour12 === 0) hour12 = 12;

        return day + ' ' + hour12 + ':' + m + suffix;
    },
};

/**
 * Actions reachable from a data-action attribute.
 *
 * An explicit allowlist, not App[name]: dispatching a DOM-supplied string into
 * an arbitrary property of App would make every method — including future ones
 * nobody vetted for this — reachable from markup. This maps only what the page
 * actually needs, and an unknown action is a no-op rather than a lookup.
 */
const ACTIONS = {
    adjustSetting: (...a) => App.adjustSetting(...a),
    cancelAddAccount: () => App.cancelAddAccount(),
    cancelDisconnectGdrive: () => App.cancelDisconnectGdrive(),
    cancelRemoveAccount: () => App.cancelRemoveAccount(),
    confirmDisconnectGdrive: () => App.confirmDisconnectGdrive(),
    confirmRemoveAccount: (...a) => App.confirmRemoveAccount(...a),
    connectOAuth: (...a) => App.connectOAuth(...a),
    editSection: (...a) => App.editSection(...a),
    executeDisconnectGdrive: () => App.executeDisconnectGdrive(),
    executeRemoveAccount: () => App.executeRemoveAccount(),
    finishSetup: () => App.finishSetup(),
    goToStep: (...a) => App.goToStep(...a),
    reloadPage: () => location.reload(),
    returnToHome: () => App.returnToHome(),
    runFullSetup: () => App.runFullSetup(),
    saveSchedule: () => App.saveSchedule(),
    saveScheduleAndReturn: () => App.saveScheduleAndReturn(),
    selectOption: (...a) => App.selectOption(...a),
    selectWindow: (...a) => App.selectWindow(...a),
    showAddAccountForm: () => App.showAddAccountForm(),
    startIndexing: () => App.startIndexing(),
    submitAddAccount: () => App.submitAddAccount(),
    switchAccount: (...a) => App.switchAccount(...a),
    syncMedia: () => App.syncMedia(),
    toggleCard: (...a) => App.toggleCard(...a),
    toggleSetting: (...a) => App.toggleSetting(...a),
    validateFolder: () => App.validateFolder(),
};

/**
 * Run the action declared on the nearest ancestor carrying one.
 *
 * `eventName` is matched against the element's declared data-event (default
 * 'click'). Without that match a checkbox toggle would fire twice — once from
 * the click on the input, once from the change it triggers — and net to no
 * change at all.
 *
 * Args ride in data-args as JSON so types survive the DOM: selectWindow(9, 21)
 * needs numbers, adjustSetting('posts_per_day', 1) needs a string then a number.
 */
function _runDeclaredAction(event, eventName) {
    const el = event.target.closest('[data-action]');
    if (!el) return;
    if ((el.dataset.event || 'click') !== eventName) return;

    const fn = ACTIONS[el.dataset.action];
    if (!fn) return;

    let args = [];
    const raw = el.dataset.args;
    if (raw) {
        try {
            args = JSON.parse(raw);
        } catch (err) {
            return;
        }
    }
    fn(...args);
}

// Delegated listeners replace inline on* attributes, which the Mini App's CSP
// (script-src without 'unsafe-inline') refuses to execute. See #879.
document.addEventListener('click', e => _runDeclaredAction(e, 'click'));
document.addEventListener('change', e => _runDeclaredAction(e, 'change'));

// Start the app when DOM is ready
document.addEventListener('DOMContentLoaded', () => App.init());
