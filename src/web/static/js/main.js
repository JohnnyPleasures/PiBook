// PiBook Web Interface - Main JavaScript

// Navigation
let epubPreparationStatusTimer = null;

let systemStatsTimer = null;
let systemStatsPowerProfile = 'battery';

const SYSTEM_STATS_INTERVALS = Object.freeze({
    mains: 30000,
    battery: 60000,
    powersave: 180000
});

function isInfoSectionActive() {
    const info = document.getElementById('info');
    return Boolean(info && info.classList.contains('active'));
}

function systemStatsPollingAllowed() {
    return (
        document.visibilityState === 'visible'
        && isInfoSectionActive()
    );
}

function stopSystemStatsPolling() {
    if (systemStatsTimer !== null) {
        clearTimeout(systemStatsTimer);
        systemStatsTimer = null;
    }
}

function scheduleSystemStatsPolling() {
    stopSystemStatsPolling();

    if (!systemStatsPollingAllowed()) return;

    const delay =
        SYSTEM_STATS_INTERVALS[systemStatsPowerProfile]
        || SYSTEM_STATS_INTERVALS.battery;

    systemStatsTimer = setTimeout(() => {
        systemStatsTimer = null;

        if (!systemStatsPollingAllowed()) return;

        refreshSystemStats().finally(() => {
            scheduleSystemStatsPolling();
        });
    }, delay);
}

function syncSystemStatsPolling({ refresh = false } = {}) {
    stopSystemStatsPolling();

    if (!systemStatsPollingAllowed()) return;

    if (refresh) {
        refreshSystemStats().finally(() => {
            scheduleSystemStatsPolling();
        });
    } else {
        scheduleSystemStatsPolling();
    }
}

document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        syncSystemStatsPolling({ refresh: true });
    } else {
        stopSystemStatsPolling();
    }
});

const PIBOOK_SIDEBAR_STORAGE_KEY = 'pibookSidebarCollapsed';

function setSidebarCollapsed(collapsed, persist = true) {
    document.body.classList.toggle('sidebar-collapsed', collapsed);

    const toggle = document.getElementById('sidebar-toggle');
    if (toggle) {
        toggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        toggle.setAttribute(
            'aria-label',
            collapsed ? 'Expandir menu' : 'Recolher menu'
        );

        const icon = toggle.querySelector('.sidebar-book-icon');
        if (icon) {
            icon.textContent = collapsed ? '📕' : '📖';
        }
    }

    if (persist) {
        try {
            localStorage.setItem(
                PIBOOK_SIDEBAR_STORAGE_KEY,
                collapsed ? '1' : '0'
            );
        } catch (error) {
            // Storage may be unavailable in private/restricted browser modes.
        }
    }
}

function toggleSidebar() {
    setSidebarCollapsed(
        !document.body.classList.contains('sidebar-collapsed')
    );
}

function initializeSidebar() {
    let stored = null;

    try {
        stored = localStorage.getItem(PIBOOK_SIDEBAR_STORAGE_KEY);
    } catch (error) {
        stored = null;
    }

    const collapsed = stored === null
        ? window.matchMedia('(max-width: 768px)').matches
        : stored === '1';

    setSidebarCollapsed(collapsed, false);

    document.querySelectorAll('.nav-item').forEach(item => {
        const label = item.querySelector('.nav-text');
        if (label && !item.hasAttribute('title')) {
            item.setAttribute('title', label.textContent.trim());
        }
    });
}

document.addEventListener('DOMContentLoaded', initializeSidebar);

function disablePageZoom() {
    const preventGesture = event => {
        event.preventDefault();
    };

    document.addEventListener('gesturestart', preventGesture, { passive: false });
    document.addEventListener('gesturechange', preventGesture, { passive: false });
    document.addEventListener('gestureend', preventGesture, { passive: false });

    document.addEventListener(
        'touchmove',
        event => {
            if (event.touches && event.touches.length > 1) {
                event.preventDefault();
            }
        },
        { passive: false }
    );

    document.addEventListener(
        'dblclick',
        event => {
            event.preventDefault();
        },
        { passive: false }
    );
}

document.addEventListener('DOMContentLoaded', disablePageZoom);


function switchSection(sectionId) {
    if (sectionId !== 'library') {
        stopEpubPreparationStatusPolling();
    }

    // Hide all sections
    document.querySelectorAll('.content-section').forEach(section => {
        section.classList.remove('active');
    });

    // Remove active from all nav items
    document.querySelectorAll('.nav-item').forEach(item => {
        item.classList.remove('active');
    });

    // Show selected section
    const section = document.getElementById(sectionId);
    if (section) {
        section.classList.add('active');
    }

    // Highlight active nav item
    const navItem = document.querySelector(`[data-section="${sectionId}"]`);
    if (navItem) {
        navItem.classList.add('active');
    }

    if (sectionId === 'info') {
        syncSystemStatsPolling({ refresh: true });
    } else {
        stopSystemStatsPolling();
    }

    if (sectionId === 'navigation' || sectionId === 'settings') {
        refreshSystemStats();
    } else if (sectionId === 'todo') {
        loadTodos();
    } else if (sectionId === 'ipscanner') {
        initIPScanner();
    } else if (sectionId === 'klipper') {
        initKlipper();
    } else if (sectionId === 'library') {
        refreshEpubPreparationStatuses();
        startEpubPreparationStatusPolling();
    } else if (sectionId === 'remote') {
        refreshRemoteView();
    } else if (sectionId === 'logs') {
        if (!currentLogType) {
            loadLogs('app');
        } else {
            refreshActiveLogs();
        }
    }
}

// Logs Functions
let currentLogType = null;

function loadLogs(type) {
    currentLogType = type;

    // Update active tab button style
    document.getElementById('tab-app-logs').style.background = type === 'app' ? '#2196F3' : '#e0e0e0';
    document.getElementById('tab-app-logs').style.color = type === 'app' ? '#fff' : '#333';
    document.getElementById('tab-system-logs').style.background = type === 'system' ? '#2196F3' : '#e0e0e0';
    document.getElementById('tab-system-logs').style.color = type === 'system' ? '#fff' : '#333';

    const viewer = document.getElementById('log-viewer-content');
    viewer.innerHTML = `Loading ${type} logs...`;

    fetch(`/api/logs/${type}`)
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                viewer.innerHTML = `<span style="color: #f44336;">Failed to load logs: ${escapeHtml(data.error)}</span>`;
            } else {
                viewer.innerHTML = escapeHtml(data.logs || 'No logs found.');
                // Scroll to bottom to see latest logs
                viewer.scrollTop = viewer.scrollHeight;
            }
        })
        .catch(error => {
            viewer.innerHTML = `<span style="color: #f44336;">Error fetching logs: ${escapeHtml(error.message)}</span>`;
        });
}

function refreshActiveLogs() {
    if (currentLogType) {
        loadLogs(currentLogType);
    } else {
        loadLogs('app');
    }
}

// EPUB preparation status
function formatEpubPreparationStatus(status) {
    const state = status && status.state ? status.state : 'unknown';
    const progress = Number.isFinite(Number(status && status.progress))
        ? Math.max(0, Math.min(100, Math.round(Number(status.progress))))
        : 0;

    if (state === 'ready') return '✓ Pronto';
    if (state === 'preparing') return `⏳ A preparar · ${progress}%`;
    if (state === 'pending') return '⏳ Na fila';
    if (state === 'paused') return `⏸ Pausado · ${progress}%`;
    if (state === 'error') return '⚠ Erro';
    if (state === 'not_prepared') return '○ Por preparar';
    return '… A verificar';
}

function refreshEpubPreparationStatuses() {
    const library = document.getElementById('library');
    if (!library || !library.classList.contains('active')) return;

    if (document.visibilityState !== 'visible') return;

    const elements = Array.from(
        document.querySelectorAll('.epub-preparation-status')
    );
    if (!elements.length) return;

    fetch('/api/epub/preparation', {
        headers: { 'Accept': 'application/json' }
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Falha ao obter preparação EPUB');
            }
            return data.books || {};
        })
        .then(statuses => {
            let stillWorking = false;

            elements.forEach(element => {
                const filename = element.dataset.filename;
                const status = statuses[filename] || {
                    state: 'not_prepared',
                    progress: 0
                };

                element.textContent = formatEpubPreparationStatus(status);
                element.title = status.message || '';

                if (
                    status.state === 'pending'
                    || status.state === 'preparing'
                    || status.state === 'paused'
                ) {
                    stillWorking = true;
                }
            });

            // Once everything is ready/error/not-prepared there is no reason
            // to keep waking the Pi Zero with polling.
            if (!stillWorking) {
                stopEpubPreparationStatusPolling();
            }
        })
        .catch(error => {
            console.warn('EPUB preparation status refresh failed:', error);
        });
}

function startEpubPreparationStatusPolling() {
    stopEpubPreparationStatusPolling();

    const library = document.getElementById('library');
    if (!library || !library.classList.contains('active')) return;

    epubPreparationStatusTimer = setInterval(() => {
        refreshEpubPreparationStatuses();
    }, 3000);
}

function stopEpubPreparationStatusPolling() {
    if (epubPreparationStatusTimer !== null) {
        clearInterval(epubPreparationStatusTimer);
        epubPreparationStatusTimer = null;
    }
}

// File Upload
function uploadFile(event) {
    event.preventDefault();
    const form = event.target;
    const fileInput = form.querySelector('input[type="file"]');

    if (!fileInput || !fileInput.files || fileInput.files.length === 0) {
        alert('Seleciona primeiro pelo menos um ficheiro EPUB.');
        return;
    }

    const formData = new FormData(form);
    const button = form.querySelector('button[type="submit"]');
    if (button) {
        button.disabled = true;
        button.textContent = 'A carregar...';
    }

    fetch('/upload', { method: 'POST', body: formData })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Falha ao carregar o livro');
            }
            return data;
        })
        .then(data => {
            const warning = data.errors && data.errors.length
                ? `\nAvisos: ${data.errors.join('; ')}`
                : '';
            alert(`${data.count} livro(s) carregado(s).${warning}`);
            location.hash = '#library';
            location.reload();
        })
        .catch(error => alert('Erro no carregamento: ' + error.message))
        .finally(() => {
            if (button) {
                button.disabled = false;
                button.textContent = 'Carregar';
            }
        });
}

// File Delete
function deleteFile(filename, bookTitle = filename) {
    if (!confirm(`Eliminar “${bookTitle}”?`)) return;

    fetch('/delete/' + encodeURIComponent(filename), {
        method: 'POST',
        headers: { 'Accept': 'application/json' }
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Não foi possível eliminar o livro');
            }
            return data;
        })
        .then(() => {
            location.hash = '#library';
            location.reload();
        })
        .catch(error => alert('Erro: ' + error.message));
}


function saveFieldState(form) {
    if (!form) return;
    form.querySelectorAll('input[name], select[name], textarea[name]').forEach(field => {
        field.dataset.savedValue = field.type === 'checkbox'
            ? (field.checked ? '1' : '0')
            : String(field.value);
    });
}

function getChangedFormGroups(form) {
    const groups = [];
    if (!form) return groups;

    form.querySelectorAll('input[name], select[name], textarea[name]').forEach(field => {
        const currentValue = field.type === 'checkbox'
            ? (field.checked ? '1' : '0')
            : String(field.value);

        const savedValue = field.dataset.savedValue ?? currentValue;

        if (currentValue !== savedValue) {
            const group = field.closest('.form-group') || field.parentElement || field;
            if (group && !groups.includes(group)) groups.push(group);
        }
    });

    return groups;
}

function clearSavedFieldHighlights(form) {
    if (!form) return;
    form.querySelectorAll('.saved-field-group').forEach(el => {
        el.classList.remove('saved-field-group');
    });
}

function highlightSavedFieldGroups(groups) {
    (groups || []).forEach(group => {
        group.classList.add('saved-field-group');
    });
}

function scrollSettingsToTop() {
    const settingsSection = document.getElementById('settings');
    const message = document.getElementById('settings-message');

    if (settingsSection) {
        settingsSection.scrollIntoView({
            behavior: 'smooth',
            block: 'start'
        });
    }

    window.scrollTo({ top: 0, behavior: 'smooth' });
    document.documentElement.scrollTop = 0;
    document.body.scrollTop = 0;

    if (message) {
        setTimeout(() => {
            message.scrollIntoView({
                behavior: 'smooth',
                block: 'start'
            });
        }, 120);
    }
}

// Settings Form(s)
function updatePowerSettingsVisibility() {
    const mode = document.getElementById('power_mode');
    const autoOptions = document.getElementById('auto-power-options');
    const autoPowersave = document.getElementById('auto_powersave_enabled');
    const threshold = document.getElementById('auto-powersave-threshold-group');

    if (!mode) return;

    const isAuto = mode.value === 'auto';

    if (autoOptions) {
        autoOptions.style.display = isAuto ? '' : 'none';
    }

    if (threshold) {
        threshold.style.display =
            isAuto && autoPowersave && autoPowersave.checked
                ? ''
                : 'none';
    }
}

document.addEventListener('DOMContentLoaded', function () {
    const powerMode = document.getElementById('power_mode');
    const autoPowersave = document.getElementById('auto_powersave_enabled');

    if (powerMode) {
        powerMode.addEventListener('change', updatePowerSettingsVisibility);
    }

    if (autoPowersave) {
        autoPowersave.addEventListener('change', updatePowerSettingsVisibility);
    }

    updatePowerSettingsVisibility();
    const settingForms = [
        { form: document.getElementById('settings-form'), msgId: 'settings-message' },
        { form: document.getElementById('reader-settings-form'), msgId: 'reader-settings-message' }
    ];

    settingForms.forEach(({ form, msgId }) => {
        if (form) {
            saveFieldState(form);
            form.addEventListener('submit', function (e) {
                e.preventDefault();

                const formData = new FormData(this);
                const data = {};

                // Convert form data to object
                for (let [key, value] of formData.entries()) {
                    if (key === 'show_page_numbers' || key === 'auto_powersave_enabled') {
                        data[key] = true;
                    } else {
                        data[key] = isNaN(value) ? value : parseFloat(value);
                    }
                }

                // Add unchecked checkboxes as false, ONLY if they exist in this specific form
                const allCheckboxIds = ['show_page_numbers', 'auto_powersave_enabled'];
                allCheckboxIds.forEach(field => {
                    const checkbox = document.getElementById(field);
                    const checkboxExistsInForm =
                        checkbox && checkbox.form === this;
                    if (checkboxExistsInForm && !(field in data)) {
                        data[field] = false; // It exists in the form, but wasn't checked
                    }
                });

                const changedGroups = getChangedFormGroups(this);

                // Save settings
                fetch('/save_settings', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify(data)
                })
                    .then(response => response.json())
                    .then(result => {
                        const messageDiv = document.getElementById(msgId);
                        if (result.status === 'success') {
                            messageDiv.className = 'message success';
                            messageDiv.innerHTML = '<strong>✓ Definições guardadas.</strong><br>As alterações compatíveis foram aplicadas de imediato.';
                            messageDiv.style.display = 'block';

                            clearSavedFieldHighlights(this);
                            highlightSavedFieldGroups(changedGroups);
                            saveFieldState(this);

                            if (this.id === 'settings-form') {
                                scrollSettingsToTop();
                            } else {
                                messageDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                            }

                            // Reload CPU voltage after short delay
                            setTimeout(() => {
                                fetch('/api/cpu_voltage')
                                    .then(response => response.json())
                                    .then(data => {
                                        const statusEl = document.getElementById('voltage-status');
                                        if (statusEl && data.voltage) {
                                            statusEl.textContent = 'Current CPU Voltage: ' + data.voltage;
                                        }
                                    });
                            }, 500);
                        } else {
                            throw new Error(result.error || 'Save failed');
                        }
                    })
                    .catch(error => {
                        const messageDiv = document.getElementById(msgId);
                        messageDiv.className = 'message error';
                        messageDiv.innerHTML = '<strong>❌ Erro ao guardar.</strong><br>' + error.message;
                        messageDiv.style.display = 'block';
                        messageDiv.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
                    });
            });
        }
    });

    // Load current CPU voltage
    fetch('/api/cpu_voltage')
        .then(response => response.json())
        .then(data => {
            const statusEl = document.getElementById('voltage-status');
            if (statusEl && data.voltage) {
                statusEl.textContent = 'Current CPU Voltage: ' + data.voltage;
            } else if (statusEl) {
                statusEl.textContent = 'Current CPU Voltage: Unable to read';
            }
        })
        .catch(err => {
            const statusEl = document.getElementById('voltage-status');
            if (statusEl) {
                statusEl.textContent = 'Current CPU Voltage: Error loading';
            }
        });
});

// Terminal Functions - PiBook Terminal Web v2
let terminalRunning = false;
let terminalCommandId = null;
let terminalPersistTimer = null;

const TERMINAL_HISTORY_KEY = 'pibook_terminal_history_v2';
const TERMINAL_OUTPUT_KEY = 'pibook_terminal_output_v2';
const TERMINAL_MAX_HISTORY = 20;
const TERMINAL_MAX_SAVED_OUTPUT = 180000;


function terminalGet(id) {
    return document.getElementById(id);
}


function terminalSetState(text, state = 'idle') {
    const el = terminalGet('terminal-state');
    if (!el) return;

    el.textContent = text;
    el.className = 'terminal-state terminal-state-' + state;
}


function setTerminalRunning(running) {
    terminalRunning = running;

    const executeBtn = terminalGet('terminal-execute');
    const stopBtn = terminalGet('terminal-stop');
    const input = terminalGet('terminal-input');

    if (executeBtn) {
        executeBtn.disabled = running;
        executeBtn.textContent = running ? 'A executar…' : 'Executar';
    }

    if (stopBtn) {
        stopBtn.disabled = !running;
    }

    if (input) {
        input.readOnly = running;
    }
}


function scheduleTerminalOutputSave() {
    clearTimeout(terminalPersistTimer);

    terminalPersistTimer = setTimeout(() => {
        try {
            const output = terminalGet('terminal-output');
            if (!output) return;

            const text = output.textContent || '';
            const saved = text.slice(-TERMINAL_MAX_SAVED_OUTPUT);
            localStorage.setItem(TERMINAL_OUTPUT_KEY, saved);
        } catch (e) {
            // localStorage may be unavailable in some browser modes.
        }
    }, 120);
}


function appendToTerminal(text, addNewline = false) {
    const output = terminalGet('terminal-output');
    if (!output) return;

    output.textContent += String(text) + (addNewline ? '\n' : '');
    output.scrollTop = output.scrollHeight;

    scheduleTerminalOutputSave();
}


function clearTerminal() {
    const output = terminalGet('terminal-output');
    if (!output) return;

    output.textContent = 'PiBook Terminal v2\n';
    scheduleTerminalOutputSave();
}


function setCommand(cmd) {
    const input = terminalGet('terminal-input');
    if (!input) return;

    input.value = cmd || '';
    input.focus();

    if (window.innerWidth < 700) {
        input.scrollIntoView({
            behavior: 'smooth',
            block: 'center'
        });
    }
}


function terminalLoadHistory() {
    try {
        const parsed = JSON.parse(
            localStorage.getItem(TERMINAL_HISTORY_KEY) || '[]'
        );

        return Array.isArray(parsed) ? parsed : [];
    } catch (e) {
        return [];
    }
}


function terminalSaveHistory(history) {
    try {
        localStorage.setItem(
            TERMINAL_HISTORY_KEY,
            JSON.stringify(history.slice(0, TERMINAL_MAX_HISTORY))
        );
    } catch (e) {
        // Ignore storage failures.
    }
}


function addTerminalHistory(command) {
    let history = terminalLoadHistory();

    history = history.filter(item => item !== command);
    history.unshift(command);

    history = history
        .filter(item => typeof item === 'string' && item.trim())
        .map(item => item.slice(0, 12000))
        .slice(0, TERMINAL_MAX_HISTORY);

    terminalSaveHistory(history);
    renderTerminalHistory();
}


function renderTerminalHistory() {
    const select = terminalGet('terminal-history');
    if (!select) return;

    const history = terminalLoadHistory();

    select.innerHTML = '';

    const first = document.createElement('option');
    first.value = '';
    first.textContent = history.length
        ? `Histórico (${history.length})`
        : 'Histórico';

    select.appendChild(first);

    history.forEach((command, index) => {
        const option = document.createElement('option');
        option.value = command;

        const firstLine = command.split('\n')[0].trim();
        let label = firstLine || '(bloco multilinha)';

        if (command.includes('\n')) {
            label += ' …';
        }

        if (label.length > 72) {
            label = label.slice(0, 69) + '…';
        }

        option.textContent = `${index + 1}. ${label}`;
        select.appendChild(option);
    });
}


function clearTerminalHistory() {
    try {
        localStorage.removeItem(TERMINAL_HISTORY_KEY);
    } catch (e) {
        // Ignore.
    }

    renderTerminalHistory();
}


function terminalCommandIdNew() {
    return 'cmd_' +
        Date.now().toString(36) +
        '_' +
        Math.random().toString(36).slice(2, 10);
}


function terminalIsPiBookRestart(command) {
    const normalized = command.trim().replace(/\s+/g, ' ');

    return normalized === 'sudo systemctl restart pibook-zero.service' ||
           normalized === 'sudo systemctl restart pibook-zero';
}


function handleTerminalEvent(data) {
    if (!data || typeof data !== 'object') {
        return;
    }

    if (data.type === 'started') {
        terminalSetState(
            data.pid ? `A executar · PID ${data.pid}` : 'A executar',
            'running'
        );
        return;
    }

    if (data.type === 'stdout') {
        appendToTerminal(data.stdout || '');
        return;
    }

    if (data.type === 'finished') {
        const rc = Number(data.returncode);
        const duration = data.duration !== undefined
            ? `${data.duration}s`
            : '?';

        appendToTerminal(
            `\n[Concluído · código ${rc} · ${duration}]\n`
        );

        setTerminalRunning(false);

        if (rc === 0) {
            terminalSetState('Concluído', 'ok');
        } else {
            terminalSetState(`Erro · código ${rc}`, 'error');
        }

        if (data.restart_requested && rc === 0) {
            terminalBeginReconnect();
        }

        return;
    }

    if (data.type === 'error' || data.error) {
        const message = data.error || 'Erro desconhecido';

        appendToTerminal(`\n[Erro] ${message}\n`);
        setTerminalRunning(false);
        terminalSetState('Erro', 'error');
    }
}


async function executeCommand() {
    if (terminalRunning) {
        return;
    }

    const input = terminalGet('terminal-input');
    if (!input) return;

    const command = input.value.trim();

    if (!command) {
        terminalSetState('Sem comando', 'error');
        input.focus();
        return;
    }

    terminalCommandId = terminalCommandIdNew();
    const restartExpected = terminalIsPiBookRestart(command);

    addTerminalHistory(command);

    // Cada nova execução começa com um output limpo.
    // O histórico de comandos permanece guardado separadamente.
    clearTerminal();

    // O comando já ficou guardado no histórico e será mostrado no output.
    // Libertar imediatamente a caixa para o próximo comando.
    input.value = '';

    appendToTerminal(
        `\npi@pibook:~/PiBook$ ${command}\n`
    );

    setTerminalRunning(true);
    terminalSetState('A iniciar…', 'running');

    try {
        const response = await fetch('/terminal/execute', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'Accept': 'text/event-stream'
            },
            cache: 'no-store',
            body: JSON.stringify({
                command: command,
                command_id: terminalCommandId
            })
        });

        if (!response.ok) {
            let message = `HTTP ${response.status}`;

            try {
                const data = await response.json();
                if (data.error) {
                    message = data.error;
                }
            } catch (e) {
                // Keep HTTP message.
            }

            throw new Error(message);
        }

        if (!response.body) {
            throw new Error(
                'O browser não disponibilizou o stream da resposta'
            );
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder('utf-8');
        let buffer = '';
        let finishedEventSeen = false;

        while (true) {
            const result = await reader.read();

            if (result.done) {
                buffer += decoder.decode();

                if (buffer.trim()) {
                    processTerminalSseBuffer(buffer, handleTerminalEvent);
                }

                break;
            }

            buffer += decoder.decode(result.value, {
                stream: true
            });

            let boundary;

            while ((boundary = buffer.indexOf('\n\n')) !== -1) {
                const eventText = buffer.slice(0, boundary);
                buffer = buffer.slice(boundary + 2);

                const eventData = parseTerminalSseEvent(eventText);

                if (eventData) {
                    if (
                        eventData.type === 'finished' ||
                        eventData.type === 'error' ||
                        eventData.error
                    ) {
                        finishedEventSeen = true;
                    }

                    handleTerminalEvent(eventData);
                }
            }
        }

        if (terminalRunning && !finishedEventSeen) {
            setTerminalRunning(false);

            if (restartExpected) {
                terminalBeginReconnect();
            } else {
                appendToTerminal(
                    '\n[A ligação ao stream terminou antes da confirmação final]\n'
                );
                terminalSetState('Ligação terminada', 'error');
            }
        }

    } catch (error) {
        if (restartExpected) {
            appendToTerminal(
                '\n[A ligação caiu durante o reinício do PiBook]\n'
            );
            setTerminalRunning(false);
            terminalBeginReconnect();
            return;
        }

        appendToTerminal(
            `\n[Erro de ligação] ${error.message}\n`
        );

        setTerminalRunning(false);
        terminalSetState('Erro de ligação', 'error');
    }
}


function parseTerminalSseEvent(eventText) {
    const lines = eventText.split('\n');
    const dataLines = [];

    lines.forEach(line => {
        if (line.startsWith('data:')) {
            dataLines.push(
                line.slice(5).replace(/^ /, '')
            );
        }
    });

    if (!dataLines.length) {
        return null;
    }

    try {
        return JSON.parse(dataLines.join('\n'));
    } catch (e) {
        console.error(
            'PiBook Terminal: evento SSE inválido',
            eventText,
            e
        );
        return null;
    }
}


function processTerminalSseBuffer(buffer, callback) {
    buffer
        .split('\n\n')
        .map(item => item.trim())
        .filter(Boolean)
        .forEach(eventText => {
            const data = parseTerminalSseEvent(eventText);
            if (data) {
                callback(data);
            }
        });
}


async function stopTerminalCommand() {
    if (!terminalRunning || !terminalCommandId) {
        return;
    }

    const stopBtn = terminalGet('terminal-stop');

    if (stopBtn) {
        stopBtn.disabled = true;
        stopBtn.textContent = 'A parar…';
    }

    terminalSetState('A parar…', 'running');

    try {
        const response = await fetch('/terminal/stop', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            cache: 'no-store',
            body: JSON.stringify({
                command_id: terminalCommandId
            })
        });

        let data = {};

        try {
            data = await response.json();
        } catch (e) {
            // Ignore JSON failure.
        }

        if (!response.ok && response.status !== 404) {
            throw new Error(
                data.error || `HTTP ${response.status}`
            );
        }

        appendToTerminal('\n[Pedido para parar enviado]\n');

    } catch (error) {
        appendToTerminal(
            `\n[Erro ao parar] ${error.message}\n`
        );

        terminalSetState('Erro ao parar', 'error');

    } finally {
        if (stopBtn) {
            stopBtn.textContent = 'Parar';

            if (terminalRunning) {
                stopBtn.disabled = false;
            }
        }
    }
}


async function copyOutput() {
    const output = terminalGet('terminal-output');
    if (!output) return;

    const text = output.textContent || '';

    try {
        if (
            navigator.clipboard &&
            window.isSecureContext
        ) {
            await navigator.clipboard.writeText(text);
        } else {
            const textarea = document.createElement('textarea');
            textarea.value = text;
            textarea.setAttribute('readonly', '');
            textarea.style.position = 'fixed';
            textarea.style.opacity = '0';
            textarea.style.pointerEvents = 'none';

            document.body.appendChild(textarea);
            textarea.select();
            textarea.setSelectionRange(0, textarea.value.length);

            const ok = document.execCommand('copy');
            textarea.remove();

            if (!ok) {
                throw new Error('O browser recusou a cópia');
            }
        }

        terminalSetState('Output copiado', 'ok');

    } catch (error) {
        terminalSetState('Não foi possível copiar', 'error');
        appendToTerminal(
            `\n[Cópia] ${error.message}\n`
        );
    }
}


function terminalBeginReconnect() {
    setTerminalRunning(false);
    terminalSetState('PiBook a reiniciar…', 'running');

    appendToTerminal(
        '\n[PiBook a reiniciar · a página tentará voltar a ligar automaticamente]\n'
    );

    let attempts = 0;
    const maxAttempts = 45;

    const poll = async () => {
        attempts += 1;

        try {
            const response = await fetch(
                '/?terminal_reconnect=' + Date.now(),
                {
                    method: 'GET',
                    cache: 'no-store'
                }
            );

            if (response.ok) {
                // Reabrir explicitamente o Terminal depois do restart.
                window.location.hash = '#terminal';
                window.location.reload();
                return;
            }
        } catch (e) {
            // Expected while service is restarting.
        }

        if (attempts >= maxAttempts) {
            terminalSetState(
                'PiBook ainda indisponível',
                'error'
            );

            appendToTerminal(
                '\n[Não consegui confirmar automaticamente o regresso do servidor]\n'
            );

            return;
        }

        setTimeout(poll, 2000);
    };

    setTimeout(poll, 3500);
}


/*
 * Preserve the global helper that existed in the previous main.js.
 * Other pages may potentially use it.
 */
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}


document.addEventListener('DOMContentLoaded', function () {
    const input = terminalGet('terminal-input');
    const history = terminalGet('terminal-history');
    const output = terminalGet('terminal-output');

    if (!input || !output) {
        return;
    }

    try {
        const savedOutput = localStorage.getItem(
            TERMINAL_OUTPUT_KEY
        );

        if (savedOutput) {
            output.textContent = savedOutput;
            output.scrollTop = output.scrollHeight;
        }
    } catch (e) {
        // Ignore storage failure.
    }

    renderTerminalHistory();

    input.addEventListener('keydown', function (event) {
        if (
            event.key === 'Enter' &&
            (event.ctrlKey || event.metaKey)
        ) {
            event.preventDefault();
            executeCommand();
        }
    });

    if (history) {
        history.addEventListener('change', function () {
            if (history.value) {
                setCommand(history.value);
            }

            history.selectedIndex = 0;
        });
    }
});


// System Stats
function refreshSystemStats() {
    return fetch('/api/system_stats')
        .then(response => response.json())
        .then(data => {
            if (
                data.effective_power_profile
                && Object.prototype.hasOwnProperty.call(
                    SYSTEM_STATS_INTERVALS,
                    data.effective_power_profile
                )
            ) {
                systemStatsPowerProfile =
                    data.effective_power_profile;
            }

            // Update CPU Temperature
            const tempEl = document.getElementById('cpu-temp');
            if (tempEl && data.cpu_temp) {
                tempEl.textContent = data.cpu_temp;
                // Color code based on temperature
                const temp = parseFloat(data.cpu_temp);
                if (temp < 60) {
                    tempEl.style.color = '#4CAF50'; // Green
                } else if (temp < 70) {
                    tempEl.style.color = '#ff9800'; // Orange
                } else {
                    tempEl.style.color = '#f44336'; // Red
                }
            }

            // Update CPU Voltage
            const voltageEl = document.getElementById('cpu-voltage');
            if (voltageEl && data.cpu_voltage) {
                voltageEl.textContent = data.cpu_voltage;
            }

            // Update CPU Speed
            const speedEl = document.getElementById('cpu-speed');
            if (speedEl && data.cpu_speed) {
                speedEl.textContent = data.cpu_speed;
            }

            // Update WiFi Status
            const wifiEl = document.getElementById('wifi-status');
            if (wifiEl && data.wifi_status) {
                wifiEl.textContent = data.wifi_status;
                if (data.wifi_status === 'On') {
                    wifiEl.style.color = '#4CAF50';
                } else if (data.wifi_status === 'Off') {
                    wifiEl.style.color = '#f44336';
                }
            }

            // Update Bluetooth Status
            const btEl = document.getElementById('bluetooth-status');
            if (btEl && data.bluetooth_status) {
                btEl.textContent = data.bluetooth_status;
                if (data.bluetooth_status.startsWith('On')) {
                    btEl.style.color = '#4CAF50';
                } else if (data.bluetooth_status === 'Off') {
                    btEl.style.color = '#f44336';
                }
            }

            // Update Throttle Status
            const throttleEl = document.getElementById('throttle-status');
            if (throttleEl && data.throttle_status) {
                throttleEl.textContent = data.throttle_status;
                if (data.throttle_status === 'OK') {
                    throttleEl.style.color = '#4CAF50'; // Green
                } else {
                    throttleEl.style.color = '#f44336'; // Red
                }
                throttleEl.title = data.throttle_detail || '';
            }

            // Update OS Info
            const osEl = document.getElementById('os-info');
            if (osEl && data.os_name) {
                osEl.textContent = data.os_name;
            }

            // Update Uptime
            const uptimeEl = document.getElementById('uptime');
            if (uptimeEl && data.uptime) {
                uptimeEl.textContent = data.uptime;
            }

            // Update CPU Cores
            const coresEl = document.getElementById('cpu-cores');
            if (coresEl && data.active_cores && data.total_cores) {
                coresEl.textContent = `${data.active_cores}/${data.total_cores}`;
                // Color code: green if 1 core (power saving), blue if multiple
                if (data.active_cores === 1) {
                    coresEl.style.color = '#4CAF50'; // Green - power saving mode
                } else {
                    coresEl.style.color = '#2196F3'; // Blue - normal mode
                }
            }

            // Update Memory Usage
            const memoryEl = document.getElementById('memory-usage');
            if (memoryEl && data.memory_used && data.memory_total) {
                memoryEl.textContent = `${data.memory_used} / ${data.memory_total}`;
                // Color code based on percentage if available
                if (data.memory_percent) {
                    const percent = parseInt(data.memory_percent);
                    if (percent < 70) {
                        memoryEl.style.color = '#4CAF50'; // Green
                    } else if (percent < 85) {
                        memoryEl.style.color = '#ff9800'; // Orange
                    } else {
                        memoryEl.style.color = '#f44336'; // Red
                    }
                }
            }

            // Pi Zero W model
            const modelEl = document.getElementById('pi-model');
            if (modelEl) modelEl.textContent = data.model || 'Raspberry Pi Zero W';

            // Current PiBook screen
            const screenEl = document.getElementById('current-screen');
            if (screenEl) {
                screenEl.textContent =
                    data.current_screen_label
                    || data.current_screen
                    || 'Desconhecido';
            }

            // Effective power profile
            const powerProfileEls = [
                document.getElementById('effective-power-profile'),
                document.getElementById('settings-effective-power-profile')
            ].filter(Boolean);

            if (powerProfileEls.length) {
                const profileLabels = {
                    mains: '⚡ Ligado à corrente',
                    battery: '🔋 Bateria',
                    powersave: '🌙 Poupança de bateria'
                };
                const profileText =
                    profileLabels[data.effective_power_profile]
                    || data.effective_power_profile
                    || 'Desconhecido';

                powerProfileEls.forEach(el => {
                    el.textContent = profileText;
                });
            }

            // Battery
            const batteryEl = document.getElementById('battery-percentage');
            const batteryStateEl = document.getElementById('battery-state');
            const batteryElectricalEl = document.getElementById('battery-electrical');
            const batteryRuntimeEl = document.getElementById('battery-runtime');
            if (batteryEl && data.battery_percentage !== undefined && data.battery_percentage !== null) {
                batteryEl.textContent = `${data.battery_percentage}%`;
            }
            if (batteryStateEl) {
                batteryStateEl.textContent = data.battery_charging ? '⚡ A carregar' : 'A usar a bateria';
            }
            if (batteryElectricalEl) {
                const volts = Number(data.battery_voltage);
                const current = Number(data.battery_current_ma);
                const voltageText = Number.isFinite(volts) ? `${volts.toFixed(3)} V` : '—';
                const currentText = Number.isFinite(current) ? `${current >= 0 ? '+' : ''}${current.toFixed(1)} mA` : '—';
                batteryElectricalEl.textContent = `${voltageText} / ${currentText}`;
            }

            if (batteryRuntimeEl) {
                const hours = Number(data.battery_remaining_hours);

                if (
                    data.battery_charging === false
                    && Number.isFinite(hours)
                    && hours > 0
                ) {
                    const totalMinutes = Math.max(1, Math.round(hours * 60));
                    const wholeHours = Math.floor(totalMinutes / 60);
                    const minutes = totalMinutes % 60;

                    if (wholeHours > 0 && minutes > 0) {
                        batteryRuntimeEl.textContent =
                            `≈ ${wholeHours} h ${minutes} min restantes`;
                    } else if (wholeHours > 0) {
                        batteryRuntimeEl.textContent =
                            `≈ ${wholeHours} h restantes`;
                    } else {
                        batteryRuntimeEl.textContent =
                            `≈ ${minutes} min restantes`;
                    }
                } else {
                    batteryRuntimeEl.textContent = '';
                }
            }

            // Update Disk Space
            const diskEl = document.getElementById('disk-free');
            if (diskEl && data.disk_free) {
                if (data.disk_used && data.disk_total) {
                    diskEl.textContent = `${data.disk_free} free`;
                    diskEl.title = `${data.disk_used} used of ${data.disk_total}`;
                } else {
                    diskEl.textContent = data.disk_free;
                }
            }
        })
        .catch(error => {
            console.error('Failed to load system stats:', error);
        });
}

// Remote Control Functions
function sendCommand(command) {
    showRemoteStatus('A enviar comando...', 'info');
    fetch('/control/' + encodeURIComponent(command), {
        method: 'POST',
        headers: { 'Accept': 'application/json' }
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Comando recusado');
            }
            return data;
        })
        .then(data => {
            showRemoteStatus(`Comando enviado: ${data.action}`, 'success');
            setTimeout(refreshRemoteView, 1200);
        })
        .catch(error => showRemoteStatus('Erro: ' + error.message, 'error'));
}

// To-Do List Functions
function showTodoStatus(message, type = 'info') {
    const element = document.getElementById('todo-status');
    if (!element) return;

    element.className = `message ${type}`;
    element.textContent = message;
    element.style.display = 'block';

    if (type === 'success') {
        setTimeout(() => {
            element.style.display = 'none';
        }, 4000);
    }
}

function loadTodos() {
    fetch('/api/todos')
        .then(async response => {
            const data = await response.json();
            if (!response.ok) {
                throw new Error(data.error || 'Não foi possível carregar as tarefas');
            }
            return data;
        })
        .then(data => {
            const todoList = document.getElementById('todo-list');
            if (!todoList) return;

            if (!data.tasks || data.tasks.length === 0) {
                todoList.innerHTML = `
                    <div class="todo-empty">
                        <span class="todo-empty-icon">✓</span>
                        <strong>Sem tarefas</strong>
                        <span>Adiciona uma tarefa acima para começar.</span>
                    </div>
                `;
                return;
            }

            todoList.innerHTML = data.tasks.map(task => `
                <article class="todo-item ${task.completed ? 'is-completed' : ''}">
                    <label class="todo-check">
                        <input
                            type="checkbox"
                            ${task.completed ? 'checked' : ''}
                            onchange="toggleTodo('${task.id}')"
                            aria-label="Marcar tarefa como ${task.completed ? 'por fazer' : 'concluída'}"
                        >
                    </label>

                    <div class="todo-item-content">
                        <span
                            id="task-text-${task.id}"
                            class="todo-item-text"
                        >${escapeHtml(task.text)}</span>

                        <input
                            type="text"
                            id="task-edit-${task.id}"
                            class="todo-edit-input"
                            hidden
                        >
                    </div>

                    <div class="todo-item-actions">
                        <button
                            class="btn btn-secondary todo-edit-btn"
                            id="edit-btn-${task.id}"
                            onclick="startEditTodo('${task.id}')"
                        >Editar</button>

                        <button
                            class="btn todo-save-btn"
                            id="save-btn-${task.id}"
                            onclick="saveEditTodo('${task.id}')"
                            hidden
                        >Guardar</button>

                        <button
                            class="btn btn-secondary"
                            id="cancel-btn-${task.id}"
                            onclick="cancelEditTodo('${task.id}')"
                            hidden
                        >Cancelar</button>

                        <button
                            class="btn btn-danger"
                            id="delete-btn-${task.id}"
                            onclick="deleteTodo('${task.id}')"
                        >Eliminar</button>
                    </div>
                </article>
            `).join('');
        })
        .catch(error => {
            console.error('Failed to load todos:', error);
            const todoList = document.getElementById('todo-list');
            if (todoList) {
                todoList.innerHTML =
                    '<div class="todo-empty todo-error">Erro ao carregar as tarefas.</div>';
            }
        });
}

function startEditTodo(taskId) {
    const text = document.getElementById(`task-text-${taskId}`);
    const input = document.getElementById(`task-edit-${taskId}`);

    input.value = text.textContent;

    text.hidden = true;
    input.hidden = false;

    document.getElementById(`edit-btn-${taskId}`).hidden = true;
    document.getElementById(`delete-btn-${taskId}`).hidden = true;
    document.getElementById(`save-btn-${taskId}`).hidden = false;
    document.getElementById(`cancel-btn-${taskId}`).hidden = false;

    input.focus();
    input.select();
}

function cancelEditTodo(taskId) {
    document.getElementById(`task-text-${taskId}`).hidden = false;
    document.getElementById(`task-edit-${taskId}`).hidden = true;
    document.getElementById(`edit-btn-${taskId}`).hidden = false;
    document.getElementById(`delete-btn-${taskId}`).hidden = false;
    document.getElementById(`save-btn-${taskId}`).hidden = true;
    document.getElementById(`cancel-btn-${taskId}`).hidden = true;
}

function saveEditTodo(taskId) {
    const newText = document.getElementById(`task-edit-${taskId}`).value.trim();

    if (!newText) {
        alert('A descrição da tarefa não pode ficar vazia.');
        return;
    }

    fetch(`/api/todos/${taskId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: newText })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                loadTodos();
            } else {
                alert('Não foi possível editar a tarefa: ' + (data.error || 'erro desconhecido'));
            }
        })
        .catch(error => {
            alert('Erro ao editar a tarefa: ' + error.message);
        });
}

function addTodo() {
    const input = document.getElementById('new-task-input');
    const text = input.value.trim();

    if (!text) {
        alert('Escreve primeiro uma tarefa.');
        return;
    }

    fetch('/api/todos', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                input.value = '';
                loadTodos();
            } else {
                alert('Não foi possível adicionar a tarefa: ' + (data.error || 'erro desconhecido'));
            }
        })
        .catch(error => {
            alert('Erro ao adicionar a tarefa: ' + error.message);
        });
}

function toggleTodo(taskId) {
    fetch(`/api/todos/${taskId}`, { method: 'PUT' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                loadTodos();
            } else {
                alert('Não foi possível atualizar a tarefa: ' + (data.error || 'erro desconhecido'));
            }
        })
        .catch(error => {
            alert('Erro ao atualizar a tarefa: ' + error.message);
        });
}

function deleteTodo(taskId) {
    if (!confirm('Eliminar esta tarefa?')) return;

    fetch(`/api/todos/${taskId}`, { method: 'DELETE' })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                loadTodos();
            } else {
                alert('Não foi possível eliminar a tarefa: ' + (data.error || 'erro desconhecido'));
            }
        })
        .catch(error => {
            alert('Erro ao eliminar a tarefa: ' + error.message);
        });
}

function openTodoOnEpaper() {
    showTodoStatus('A abrir o To Do no e-paper...', 'info');

    fetch('/api/menu/open', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({ screen: 'todo' })
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(data.error || 'Não foi possível abrir o To Do');
            }
            return data;
        })
        .then(() => {
            showTodoStatus('To Do aberto no e-paper.', 'success');
        })
        .catch(error => {
            showTodoStatus('Erro: ' + error.message, 'error');
        });
}

// Enter key support for todo input
document.addEventListener('DOMContentLoaded', function () {
    const todoInput = document.getElementById('new-task-input');
    if (todoInput) {
        todoInput.addEventListener('keypress', function (e) {
            if (e.key === 'Enter') {
                addTodo();
            }
        });
    }
});

// Bluetooth Management Functions
let bluetoothScanning = false;
let currentPairDevice = null;

// Load Bluetooth status on page load
document.addEventListener('DOMContentLoaded', function () {
    if (document.getElementById('bluetooth_enabled')) {
        refreshBluetoothStatus();

        // Toggle Bluetooth power
        document.getElementById('bluetooth_enabled').addEventListener('change', function () {
            toggleBluetoothPower(this.checked);
        });
    }
});

function refreshBluetoothStatus() {
    fetch('/api/bluetooth/status')
        .then(response => response.json())
        .then(data => {
            const checkbox = document.getElementById('bluetooth_enabled');
            const controls = document.getElementById('bluetooth-controls');

            checkbox.checked = data.powered;
            controls.style.display = data.powered ? 'block' : 'none';

            if (data.powered) {
                updatePairedDevices(data.paired_devices);
            }
        })
        .catch(error => console.error('Bluetooth status check failed:', error));
}

function toggleBluetoothPower(powerOn) {
    fetch('/api/bluetooth/power', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ power: powerOn })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                document.getElementById('bluetooth-controls').style.display = powerOn ? 'block' : 'none';
                if (powerOn) {
                    refreshBluetoothStatus();
                }
            } else {
                alert('Failed to toggle Bluetooth: ' + (data.error || 'Unknown error'));
                document.getElementById('bluetooth_enabled').checked = !powerOn;
            }
        })
        .catch(error => {
            console.error('Bluetooth power toggle failed:', error);
            alert('Failed to toggle Bluetooth');
            document.getElementById('bluetooth_enabled').checked = !powerOn;
        });
}

// Keep track of all devices found during this scan session
let discoveredDevices = new Map();

function toggleBluetoothScan() {
    bluetoothScanning = !bluetoothScanning;
    const btn = document.getElementById('bluetooth-scan-btn');
    const modal = document.getElementById('scan-modal');

    if (bluetoothScanning) {
        // Start Scanning
        btn.textContent = 'Scanning...';
        modal.style.display = 'block';
        discoveredDevices.clear();
        updateAvailableDevices([]); // Clear UI
    } else {
        // Stop Scanning
        btn.textContent = 'Scan for Devices';
        modal.style.display = 'none';

        // Also call backend to stop scan
        fetch('/api/bluetooth/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scan: false })
        }).catch(e => console.error(e));

        return;
    }

    fetch('/api/bluetooth/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ scan: bluetoothScanning })
    })
        .then(response => response.json())
        .then(data => {
            if (bluetoothScanning) {
                // Poll for devices every 2 seconds
                pollBluetoothDevices();
            }
        })
        .catch(error => {
            console.error('Bluetooth scan toggle failed:', error);
            // Do NOT close the modal on error, let user see it
            // bluetoothScanning = false;
            // modal.style.display = 'none';
            btn.textContent = 'Scan for Devices';

            // Show error in modal
            document.getElementById('available-devices').innerHTML =
                `<p style="color: red; text-align: center;">Scan failed: ${error.message || 'Unknown error'}</p>`;
        });
}

function pollBluetoothDevices() {
    if (!bluetoothScanning) return;

    fetch('/api/bluetooth/devices')
        .then(response => response.json())
        .then(data => {
            if (data.devices) {
                updateAvailableDevices(data.devices);
            }
            if (bluetoothScanning) {
                setTimeout(pollBluetoothDevices, 2000);
            }
        })
        .catch(error => {
            console.error('Device polling failed:', error);
            // Don't stop scanning on poll failure, just retry
            if (bluetoothScanning) {
                setTimeout(pollBluetoothDevices, 2000);
            }
        });
}

function updateAvailableDevices(devices) {
    const container = document.getElementById('available-devices');

    // Add/Update new devices
    devices.forEach(device => {
        discoveredDevices.set(device.mac, device);
    });

    if (discoveredDevices.size === 0) {
        container.innerHTML = '<p style="color: #666;">Scanning...</p>';
        return;
    }

    // Re-render full list (sorted by name)
    container.innerHTML = '';
    Array.from(discoveredDevices.values())
        .sort((a, b) => (a.name || '').localeCompare(b.name || ''))
        .forEach(device => {
            const div = document.createElement('div');
            div.style.cssText = 'padding: 12px; margin: 6px 0; border: 1px solid #eee; background: #f9f9f9; border-radius: 6px; display: flex; justify-content: space-between; align-items: center;';
            div.innerHTML = `
                <div>
                    <strong style="font-size: 1.1em;">${device.name}</strong>
                    <div style="font-size: 0.9em; color: #666; margin-top: 2px;">${device.mac}</div>
                </div>
                <button type="button" class="btn" style="padding: 8px 16px;" onclick="pairDevice('${device.mac}', '${device.name}')">Pair</button>
            `;
            container.appendChild(div);
        });
}

function updatePairedDevices(devices) {
    const container = document.getElementById('paired-devices');
    container.innerHTML = '';

    if (devices.length === 0) {
        container.innerHTML = '<p style="color: #666;">No paired devices</p>';
        return;
    }

    devices.forEach(device => {
        const div = document.createElement('div');
        div.style.cssText = 'padding: 8px; margin: 4px 0; border: 1px solid #ddd; border-radius: 4px; display: flex; justify-content: space-between; align-items: center;';
        div.innerHTML = `
            <span><strong>${device.name}</strong><br><small>${device.mac}</small></span>
            <button class="btn" style="padding: 4px 12px; background: #d32f2f;" onclick="removeDevice('${device.mac}')">Remove</button>
        `;
        container.appendChild(div);
    });
}

let currentPairingMac = null;

function pairDevice(mac, name) {
    // Show loading state
    const btn = document.activeElement;
    let originalText = 'Pair';
    if (btn && btn.tagName === 'BUTTON') {
        originalText = btn.textContent;
        btn.textContent = 'Pairing...';
        btn.disabled = true;
    }

    // Stop scanning to ensure stable pairing
    if (bluetoothScanning) {
        fetch('/api/bluetooth/scan', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scan: false })
        }).catch(e => console.error('Failed to stop scan:', e));
    }

    currentPairingMac = mac;

    fetch('/api/bluetooth/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: mac })
    })
        .then(response => response.json())
        .then(data => {
            // Restore button provided it still exists
            const currentBtn = document.querySelector(`button[onclick*="${mac}"]`);
            if (currentBtn) {
                currentBtn.disabled = false;
                currentBtn.textContent = 'Pair';
            }

            if (data.success) {
                if (data.status === 'passkey_required') {
                    showPasskeyModal(data.passkey, data.message);
                } else {
                    alert('Pairing successful!');
                    refreshBluetoothStatus();
                }
            } else {
                // Return to legacy PIN input behavior on failure
                showPinInputModal(name);
            }
        })
        .catch(error => {
            console.error('Pairing failed:', error);
            const currentBtn = document.querySelector(`button[onclick*="${mac}"]`);
            if (currentBtn) {
                currentBtn.disabled = false;
                currentBtn.textContent = 'Pair';
            }
            alert('Pairing request failed. You can try manual PIN entry.');
            showPinInputModal(name);
        });
}

function showPinInputModal(name) {
    document.getElementById('modal-title').innerText = 'Enter PIN';
    document.getElementById('modal-message').innerText = name ? `Enter PIN for ${name}:` : 'Enter PIN for device:';
    document.getElementById('pin-input-container').style.display = 'block';
    document.getElementById('passkey-display-container').style.display = 'none';
    document.getElementById('modal-submit-btn').style.display = 'block';
    document.getElementById('pin-input').value = '';
    document.getElementById('pin-modal').style.display = 'block';
}

function showPasskeyModal(passkey, message) {
    document.getElementById('modal-title').innerText = 'Pairing Code';
    document.getElementById('modal-message').innerText = message;
    document.getElementById('pin-input-container').style.display = 'none';
    document.getElementById('passkey-display-container').style.display = 'block';
    document.getElementById('passkey-display').innerText = passkey;
    document.getElementById('modal-submit-btn').style.display = 'none';
    document.getElementById('pin-modal').style.display = 'block';
}

function closePinModal() {
    document.getElementById('pin-modal').style.display = 'none';
    currentPairingMac = null;
}

function submitPin() {
    const pin = document.getElementById('pin-input').value;
    if (!pin) return;

    fetch('/api/bluetooth/pair', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: currentPairingMac, pin: pin })
    })
        .then(response => response.json())
        .then(data => {
            closePinModal();
            if (data.success) {
                alert('Pairing successful!');
                refreshBluetoothStatus();
            } else {
                alert('Pairing failed: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Pairing failed:', error);
            alert('Pairing failed');
            closePinModal();
        });
}


function removeDevice(mac) {
    if (!confirm('Remove this device?')) return;

    fetch('/api/bluetooth/remove', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mac: mac })
    })
        .then(response => response.json())
        .then(data => {
            if (data.success) {
                refreshBluetoothStatus();
            } else {
                alert('Failed to remove device: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(error => {
            console.error('Device removal failed:', error);
            alert('Failed to remove device');
        });
}

// Reading Progress Functions
function loadReadingProgress() {
    const container = document.getElementById('progress-list-container');
    if (!container) return;

    container.innerHTML = '<p style="color:#888;">A carregar...</p>';

    fetch('/api/progress/list')
        .then(response => response.json())
        .then(data => {
            if (data.error) {
                container.innerHTML =
                    '<p style="color:#c00;">Erro: ' +
                    escapeHtml(data.error) +
                    '</p>';
                return;
            }

            if (!data.progress || data.progress.length === 0) {
                container.innerHTML =
                    '<p style="color:#888;">Não existem posições de leitura guardadas.</p>';
                return;
            }

            let html = '<div class="reading-progress-list">';

            data.progress.forEach(function (item) {
                const currentPage = Number(item.current_page) || 0;
                const totalPages = Number(item.total_pages) || 0;

                const pct = totalPages > 0
                    ? Math.max(
                        0,
                        Math.min(
                            100,
                            Math.round((currentPage / totalPages) * 100)
                        )
                    )
                    : 0;

                const fallbackName = String(item.filename || '')
                    .replace(/\.epub$/i, '');

                const displayName = item.title || fallbackName || 'Livro';
                const safePath = encodeURIComponent(item.path || '')
                    .replace(/'/g, '%27');

                html += '<article class="reading-progress-card">';
                html += '  <div class="reading-progress-top">';
                html += '    <div class="reading-progress-book">';
                html += '      <span class="reading-progress-icon">📖</span>';
                html += '      <strong>' + escapeHtml(displayName) + '</strong>';
                html += '    </div>';
                html += '    <span class="reading-progress-percent">' + pct + '%</span>';
                html += '  </div>';

                html += '  <div class="reading-progress-bar" aria-hidden="true">';
                html += '    <span style="width:' + pct + '%"></span>';
                html += '  </div>';

                html += '  <div class="reading-progress-bottom">';
                html += '    <span>Página ' +
                    currentPage +
                    ' de ' +
                    totalPages +
                    '</span>';

                html += '    <button ' +
                    'class="btn btn-danger reading-progress-reset" ' +
                    'data-path="' + escapeHtml(safePath) + '" ' +
                    'onclick="resetBookProgress(this.dataset.path)">' +
                    'Repor' +
                    '</button>';

                html += '  </div>';
                html += '</article>';
            });

            html += '</div>';
            container.innerHTML = html;
        })
        .catch(function (err) {
            container.innerHTML =
                '<p style="color:#c00;">Erro ao carregar posições: ' +
                escapeHtml(err.message) +
                '</p>';
        });
}

function resetLibraryBookPosition(bookPath, bookTitle) {
    if (!confirm(
        `Repor “${bookTitle}” na página 1? A avaliação, o histórico e o tempo de leitura serão preservados.`
    )) return;

    fetch('/api/progress/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: bookPath })
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || data.status !== 'success') {
                throw new Error(data.error || 'Não foi possível repor a posição');
            }
            return data;
        })
        .then(data => {
            alert(data.message);
            location.hash = '#library';
            location.reload();
        })
        .catch(error => {
            alert('Erro: ' + error.message);
        });
}

function rereadBook(filename, bookTitle = filename) {
    if (!confirm(
        `Iniciar uma nova leitura de “${bookTitle}” desde a página 1? A avaliação e o histórico anterior serão preservados.`
    )) return;

    fetch('/api/books/reread', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({ filename })
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) {
                throw new Error(
                    data.error || 'Não foi possível iniciar a releitura'
                );
            }
            return data;
        })
        .then(() => {
            alert(
                'Releitura a iniciar no e-paper. O livro passará para “A reler”.'
            );
        })
        .catch(error => {
            alert('Erro: ' + error.message);
        });
}

function resetBookProgress(encodedPath) {
    if (!confirm('Reset reading position for this book?')) return;
    const bookPath = decodeURIComponent(encodedPath);

    fetch('/api/progress/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: bookPath })
    })
        .then(response => response.json())
        .then(function (data) {
            showProgressMessage(
                data.status === 'success' ? '✓ ' + data.message : '❌ ' + (data.error || 'Unknown error'),
                data.status === 'success'
            );
            loadReadingProgress();
        })
        .catch(function (err) { showProgressMessage('❌ Error: ' + err.message, false); });
}

function resetAllLibraryPositions() {
    if (!confirm(
        'Repor na página 1 todas as leituras ativas? Livros concluídos, avaliações, histórico e tempos serão preservados.'
    )) return;

    fetch('/api/progress/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: '__all__' })
    })
        .then(async response => {
            const data = await response.json();

            if (!response.ok || data.status !== 'success') {
                throw new Error(
                    data.error || 'Não foi possível repor as posições'
                );
            }

            return data;
        })
        .then(data => {
            alert(data.message);
            location.hash = '#library';
            location.reload();
        })
        .catch(error => {
            alert('Erro: ' + error.message);
        });
}

function resetAllProgress() {
    if (!confirm('Reset ALL saved reading positions? This cannot be undone.')) return;

    fetch('/api/progress/reset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: '__all__' })
    })
        .then(response => response.json())
        .then(function (data) {
            showProgressMessage(
                data.status === 'success' ? '✓ ' + data.message : '❌ ' + (data.error || 'Unknown error'),
                data.status === 'success'
            );
            loadReadingProgress();
        })
        .catch(function (err) { showProgressMessage('❌ Error: ' + err.message, false); });
}

function showProgressMessage(msg, success) {
    const el = document.getElementById('progress-message');
    if (!el) return;
    el.className = success ? 'message success' : 'message error';
    el.textContent = msg;
    el.style.display = 'block';
    setTimeout(function () { el.style.display = 'none'; }, 4000);
}

// PiBook Zero W additions
const PIBOOK_REMOTE_PREVIEW_STORAGE_KEY =
    'pibook_remote_preview_enabled';

let remotePreviewEnabled = true;
let remotePreviewPreferenceLoaded = false;

function loadRemotePreviewPreference() {
    if (remotePreviewPreferenceLoaded) return;

    remotePreviewPreferenceLoaded = true;

    try {
        const stored = localStorage.getItem(
            PIBOOK_REMOTE_PREVIEW_STORAGE_KEY
        );

        if (stored !== null) {
            remotePreviewEnabled = stored !== '0';
        }
    } catch (error) {
        remotePreviewEnabled = true;
    }

    applyRemotePreviewState();
}

function applyRemotePreviewState() {
    const toggle = document.getElementById(
        'remote-preview-toggle'
    );
    const label = document.getElementById(
        'remote-preview-toggle-label'
    );
    const container = document.getElementById(
        'remote-preview-container'
    );
    const off = document.getElementById(
        'remote-preview-off'
    );

    if (toggle) toggle.checked = remotePreviewEnabled;
    if (label) label.textContent =
        remotePreviewEnabled ? 'ON' : 'OFF';

    if (container) container.hidden = !remotePreviewEnabled;
    if (off) off.hidden = remotePreviewEnabled;
}

function setRemotePreviewEnabled(enabled) {
    remotePreviewEnabled = Boolean(enabled);
    remotePreviewPreferenceLoaded = true;

    try {
        localStorage.setItem(
            PIBOOK_REMOTE_PREVIEW_STORAGE_KEY,
            remotePreviewEnabled ? '1' : '0'
        );
    } catch (error) {
        // Preference persistence is optional.
    }

    applyRemotePreviewState();

    if (remotePreviewEnabled) {
        refreshRemoteView();
    } else {
        refreshReaderState();
    }
}

function showRemoteStatus(message, type = 'info') {
    const element = document.getElementById('remote-status');
    if (!element) return;
    element.style.display = 'block';
    element.className = `message ${type}`;
    element.textContent = message;
}

function openMenuAppDirect(screen, label) {
    const isShutdown = screen === 'shutdown';

    if (
        isShutdown
        && !confirm('Desligar o PiBook?')
    ) {
        return;
    }

    showRemoteStatus(
        isShutdown
            ? 'A desligar o PiBook...'
            : `A abrir ${label}...`,
        'info'
    );

    fetch('/api/menu/open', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        },
        body: JSON.stringify({ screen })
    })
        .then(async response => {
            const data = await response.json();

            if (!response.ok || !data.success) {
                throw new Error(
                    data.error || 'Não foi possível abrir a opção'
                );
            }

            return data;
        })
        .then(() => {
            if (isShutdown) {
                showRemoteStatus(
                    'Shutdown aceite. O PiBook vai desligar.',
                    'success'
                );
                return;
            }

            showRemoteStatus(
                `${label} aberto no e-paper.`,
                'success'
            );

            return refreshRemoteView();
        })
        .catch(error => {
            showRemoteStatus(
                'Erro: ' + error.message,
                'error'
            );
        });
}

function refreshDisplayPreview() {
    loadRemotePreviewPreference();

    if (!remotePreviewEnabled) {
        return Promise.resolve(null);
    }

    const image = document.getElementById('remote-display-preview');
    if (!image) return Promise.resolve(null);

    image.classList.add('is-loading');

    return new Promise((resolve, reject) => {
        const nextImage = new Image();

        nextImage.onload = () => {
            image.src = nextImage.src;
            image.classList.remove('is-loading');
            resolve();
        };

        nextImage.onerror = () => {
            image.classList.remove('is-loading');
            reject(new Error(
                'Não foi possível obter a visualização do e-paper'
            ));
        };

        nextImage.src = '/api/display/preview?t=' + Date.now();
    });
}

function refreshReaderState() {
    return fetch('/api/state')
        .then(async response => {
            const data = await response.json();

            if (!response.ok) {
                throw new Error(
                    data.error || 'Não foi possível obter o estado'
                );
            }

            return data;
        })
        .then(data => {
            const screenName =
                data.screen_label
                || data.screen
                || 'Desconhecido';

            let message = `Ecrã: ${screenName}`;

            // Book/page describe the warm Reader kept in memory.
            // Only expose them as current-screen information while the
            // Reader itself is actually visible on the e-paper.
            if (data.screen === 'reader') {
                if (data.book) {
                    message += ` | Livro: ${data.book}`;
                }

                if (data.page) {
                    message += ` | Página: ${data.page}`;
                    if (data.total_pages) {
                        message += `/${data.total_pages}`;
                    }
                }
            }

            showRemoteStatus(message, 'success');
            return data;
        })
        .catch(error => {
            showRemoteStatus(
                'Erro ao obter estado: ' + error.message,
                'error'
            );
            throw error;
        });
}

function refreshRemoteView() {
    loadRemotePreviewPreference();

    const stateRequest = refreshReaderState();
    const previewRequest = refreshDisplayPreview()
        .catch(error => {
            showRemoteStatus(
                'Erro na visualização: ' + error.message,
                'error'
            );
            throw error;
        });

    return Promise.allSettled([
        stateRequest,
        previewRequest,
    ]);
}

function openBook(filename, bookTitle = filename) {
    if (!confirm(`Abrir “${bookTitle}” no e-paper?`)) return;
    showRemoteStatus('A abrir o livro. Pode demorar alguns segundos...', 'info');
    fetch('/api/books/open', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ filename })
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Não foi possível abrir o livro');
            return data;
        })
        .then(() => {
            showRemoteStatus('Livro a abrir no e-paper...', 'success');
            setTimeout(refreshReaderState, 5000);
        })
        .catch(error => showRemoteStatus('Erro: ' + error.message, 'error'));
}

function renameBook(oldName, bookTitle = oldName) {
    const currentStem = oldName.toLowerCase().endsWith('.epub')
        ? oldName.slice(0, -5)
        : oldName;
    const entered = prompt(
        `Renomear “${bookTitle}”\nNovo nome do ficheiro:`,
        currentStem
    );
    if (entered === null) return;
    const newName = entered.trim();
    if (!newName) {
        alert('O nome não pode ficar vazio.');
        return;
    }

    fetch('/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
        body: JSON.stringify({ old_name: oldName, new_name: newName })
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || 'Não foi possível mudar o nome');
            return data;
        })
        .then(() => {
            location.hash = '#library';
            location.reload();
        })
        .catch(error => alert('Erro: ' + error.message));
}

function systemPower(action) {
    const isReboot = action === 'reboot';
    const label = isReboot ? 'reiniciar' : 'desligar';
    if (!confirm(`Queres mesmo ${label} o PiBook? O e-paper será limpo primeiro.`)) return;

    fetch(`/api/system/${action}`, {
        method: 'POST',
        headers: { 'Accept': 'application/json' }
    })
        .then(async response => {
            const data = await response.json();
            if (!response.ok || !data.success) throw new Error(data.error || `Não foi possível ${label}`);
            alert(isReboot
                ? 'O PiBook está a reiniciar. Aguarda cerca de 40 segundos.'
                : 'O PiBook está a desligar e vai limpar o e-paper.');
        })
        .catch(error => alert('Erro: ' + error.message));
}

function resetPiBookSettingsDefaults() {
    if (!confirm('Repor todas as definições para os valores padrão? Ainda terás de carregar em Guardar definições.')) {
        return;
    }

    const values = {
        zoom: 1.0,
        items_per_page: 4,
        library_font_size: 20,

        power_mode: 'auto',
        auto_powersave_threshold: 30,

        profile_mains_sleep: 'auto',
        profile_mains_sleep_timeout: 900,
        profile_mains_network_reading: 'auto',
        profile_mains_network_sleep: 'auto',
        profile_mains_reader_prefetch: 10,

        profile_battery_sleep: 'auto',
        profile_battery_sleep_timeout: 300,
        profile_battery_network_reading: 'auto',
        profile_battery_network_sleep: 'auto',
        profile_battery_reader_prefetch: 3,

        profile_powersave_sleep: 'auto',
        profile_powersave_sleep_timeout: 120,
        profile_powersave_network_reading: 'auto',
        profile_powersave_network_sleep: 'auto',
        profile_powersave_reader_prefetch: 1,

        sleep_message: "Shh I'm sleeping",
        shutdown_message: "OFF"
    };

    const checks = {
        show_page_numbers: true,
        auto_powersave_enabled: true
    };

    Object.entries(values).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (el) el.value = value;
    });

    Object.entries(checks).forEach(([id, value]) => {
        const el = document.getElementById(id);
        if (el) el.checked = value;
    });

    updatePowerSettingsVisibility();

    const message = document.getElementById('settings-message');
    if (message) {
        message.className = 'message info';
        message.textContent =
            'Predefinições carregadas. Carrega em Guardar definições para aplicar.';
        message.style.display = 'block';
        scrollSettingsToTop();
    }
}

function updateEpubUploadSelection(input) {
    const status = document.getElementById('epub-upload-selection');
    if (!status) return;

    const files = Array.from(input.files || []);

    if (!files.length) {
        status.textContent = 'Nenhum EPUB selecionado';
        return;
    }

    if (files.length === 1) {
        status.textContent = 'Selecionado: ' + files[0].name;
        return;
    }

    status.textContent = `${files.length} EPUBs selecionados`;
}

document.addEventListener('DOMContentLoaded', function () {
    const profileCards = Array.from(
        document.querySelectorAll('#settings .power-profile-card')
    );

    profileCards.forEach(card => {
        card.open = false;

        card.addEventListener('toggle', function () {
            if (!card.open) return;

            profileCards.forEach(other => {
                if (other !== card) {
                    other.open = false;
                }
            });
        });
    });
});
