// PiBook Wi-Fi and local-network page
(() => {
    'use strict';

    const NETWORK_PENDING_KEY = 'pibook-network-pending-v1';
    const NETWORK_PENDING_MAX_AGE = 12 * 60 * 1000;
    const WIFI_SCAN_SESSION_KEY = 'pibook-wifi-scan-session-v1';
    let networkPageInitialized = false;
    let networkStatusTimer = null;
    let networkStatusRequest = null;
    let lastNetworkStatusRefresh = 0;
    let localIPScanInterval = null;
    let latestNetworkPayload = null;

    function element(id) {
        return document.getElementById(id);
    }

    function isNetworkSectionActive() {
        const section = element('ipscanner');
        return Boolean(section && section.classList.contains('active'));
    }

    async function apiRequest(url, options = {}) {
        const response = await fetch(url, {
            cache: 'no-store',
            headers: {
                'Accept': 'application/json',
                ...(options.body ? {'Content-Type': 'application/json'} : {}),
                ...(options.headers || {})
            },
            ...options
        });

        let data;
        try {
            data = await response.json();
        } catch (error) {
            throw new Error('O PiBook devolveu uma resposta inválida.');
        }

        if (!response.ok || data.success === false) {
            throw new Error(data.error || `Pedido recusado (${response.status}).`);
        }

        return data;
    }

    function setMessage(message, type = 'info') {
        const box = element('network-message');
        if (!box) return;

        if (!message) {
            box.hidden = true;
            box.textContent = '';
            box.className = 'network-message';
            return;
        }

        box.textContent = message;
        box.className = `network-message ${type}`;
        box.hidden = false;
    }

    function operationLabel(operation) {
        const labels = {
            idle: 'Sem operação',
            scan_queued: 'Pesquisa agendada',
            scanning_wifi: 'A pesquisar redes',
            scanning: 'A pesquisar redes',
            restoring_hotspot: 'A restaurar hotspot',
            scan_recovery_pending: 'A recuperar hotspot',
            connect_queued: 'Ligação agendada',
            connecting_wifi: 'A ligar ao Wi-Fi',
            connect_recovery_pending: 'A recuperar hotspot',
            transition: 'Em transição'
        };
        return labels[operation] || operation || '—';
    }

    function resultLabel(result) {
        if (!result || !result.status || result.status === 'never') {
            return 'Ainda não efetuada';
        }
        if (result.status === 'success') {
            return result.ssid ? `Ligado a ${result.ssid}` : 'Ligação concluída';
        }
        if (result.status === 'failed') {
            return result.ssid ? `Falhou: ${result.ssid}` : 'Ligação falhou';
        }
        if (result.status === 'running') return 'Em curso';
        if (result.status === 'queued') return 'Agendada';
        return result.status;
    }

    function currentSSID(payload) {
        const actual = payload?.network?.actual || {};
        const result = payload?.connection?.result || {};

        if (
            actual.mode === 'wifi' &&
            result.status === 'success' &&
            result.profile_uuid &&
            result.profile_uuid === actual.wifi_uuid &&
            result.ssid
        ) {
            return result.ssid;
        }

        const activeProfile = (payload?.profiles || []).find(profile => profile.active);
        if (activeProfile?.ssid) return activeProfile.ssid;

        return actual.wifi_name || 'Wi-Fi';
    }

    function renderNetworkStatus(payload) {
        latestNetworkPayload = payload;

        const actual = payload?.network?.actual || {};
        const state = payload?.network?.state || {};
        const connection = payload?.connection || {};
        const connectResult = connection.result || {};
        const scan = payload?.scan || {};
        const webAction = payload?.web_action || {};
        const mode = actual.mode || state.actual_mode || 'unknown';
        const modeBadge = element('network-mode-badge');
        const currentName = element('network-current-name');
        const currentAddress = element('network-current-address');
        const operation = element('network-operation');
        const lastResult = element('network-last-result');
        const help = element('network-mode-help');
        const hotspotButton = element('network-hotspot-btn');
        const wifiButton = element('network-wifi-btn');
        const scanButton = element('wifi-scan-btn');

        modeBadge.className = `network-mode-badge mode-${mode}`;
        if (mode === 'wifi') {
            modeBadge.textContent = 'Wi-Fi';
            currentName.textContent = currentSSID(payload);
            help.textContent = 'O PiBook está ligado a uma rede externa. Podes pesquisar outras redes mantendo esta ligação.';
        } else if (mode === 'hotspot') {
            modeBadge.textContent = 'Hotspot';
            currentName.textContent = actual.hotspot_ssid || 'PiBook';
            help.textContent = 'Liga o telemóvel ou computador ao hotspot PiBook para pesquisar redes e configurar uma ligação externa.';
        } else {
            modeBadge.textContent = 'Transição';
            currentName.textContent = 'A alterar a ligação';
            help.textContent = 'Aguarda até a operação de rede terminar.';
        }

        const addresses = Array.isArray(actual.addresses) ? actual.addresses : [];
        currentAddress.textContent = addresses.length ? addresses.join(', ') : 'Sem endereço';
        operation.textContent = operationLabel(state.operation);
        lastResult.textContent = resultLabel(connectResult);

        const busy = Boolean(
            webAction.running ||
            connection.connect_service_active ||
            scan.scan_service_active ||
            (state.operation && state.operation !== 'idle')
        );

        hotspotButton.hidden = mode === 'hotspot';
        hotspotButton.disabled = busy;
        wifiButton.hidden = mode === 'wifi';
        wifiButton.disabled = busy || !(payload.profiles || []).length;

        scanButton.disabled = busy;
        scanButton.textContent = scan.scan_service_active
            ? 'A pesquisar…'
            : 'Pesquisar redes';

        renderScanResults(scan.results || {});
        renderProfiles(payload.profiles || [], actual.wifi_uuid || '');
        reconcilePendingAction(payload);
    }

    function createTextElement(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        node.textContent = text;
        return node;
    }

    function visibleWiFiScanTimestamp() {
        try {
            return sessionStorage.getItem(WIFI_SCAN_SESSION_KEY) || '';
        } catch (error) {
            return '';
        }
    }

    function rememberVisibleWiFiScan(timestamp) {
        if (!timestamp) return;

        try {
            sessionStorage.setItem(
                WIFI_SCAN_SESSION_KEY,
                String(timestamp)
            );
        } catch (error) {
            // A pesquisa continua funcional mesmo sem sessionStorage.
        }
    }

    function clearVisibleWiFiScan() {
        try {
            sessionStorage.removeItem(WIFI_SCAN_SESSION_KEY);
        } catch (error) {
            // Nada mais é necessário.
        }
    }

    function scanTimestamp(results) {
        return String(
            results?.generated_at ||
            results?.completed_at ||
            ''
        );
    }

    function signalLabel(signal) {
        const value = Number(signal) || 0;
        if (value >= 75) return 'Excelente';
        if (value >= 50) return 'Bom';
        if (value >= 25) return 'Fraco';
        return 'Muito fraco';
    }

    function securityForNetwork(network) {
        if (network.open) return 'open';
        const security = String(network.security || '').toUpperCase();
        if (security.includes('WPA3') || security.includes('SAE')) return 'sae';
        return 'wpa-psk';
    }

    function renderScanResults(results) {
        const list = element('wifi-network-list');
        const status = element('wifi-scan-status');
        if (!list || !status) return;

        const networks = Array.isArray(results.networks) ? results.networks : [];
        const generatedAt = scanTimestamp(results);
        const visibleTimestamp = visibleWiFiScanTimestamp();
        const pendingScan = pendingAction()?.action === 'scan';

        const currentScanVisible = Boolean(
            pendingScan ||
            (
                generatedAt &&
                visibleTimestamp &&
                generatedAt === visibleTimestamp
            )
        );

        list.replaceChildren();

        if (
            results.status === 'success' &&
            !currentScanVisible
        ) {
            status.textContent =
                'Pesquisa necessária para ver as redes disponíveis agora.';

            return;
        }

        if (results.status === 'running') {
            status.textContent =
                results.restore_mode === 'hotspot'
                    ? 'A pesquisa está em curso. O hotspot regressará automaticamente.'
                    : 'A pesquisa está em curso sem interromper a ligação Wi-Fi atual.';
        } else if (
            results.status === 'error' ||
            results.status === 'failed'
        ) {
            status.textContent =
                `A pesquisa falhou: ${results.error || 'erro desconhecido'}`;
        } else if (results.status === 'success') {
            if (pendingScan && generatedAt) {
                rememberVisibleWiFiScan(generatedAt);
            }

            const count = networks.length;
            const label = count === 1
                ? '1 rede encontrada'
                : `${count} redes encontradas`;

            const seconds = Number(results.duration_seconds);
            const duration = Number.isFinite(seconds) && seconds > 0
                ? ` · ${seconds.toLocaleString('pt-PT')} s`
                : '';

            let when = '';
            if (generatedAt) {
                const parsed = new Date(generatedAt);
                if (!Number.isNaN(parsed.getTime())) {
                    when =
                        ` · ${parsed.toLocaleString('pt-PT', {
                            day: '2-digit',
                            month: '2-digit',
                            hour: '2-digit',
                            minute: '2-digit'
                        })}`;
                }
            }

            status.textContent = `${label}${duration}${when}`;
        } else {
            status.textContent = 'Ainda não foi iniciada uma pesquisa.';
        }

        if (!networks.length) {
            const empty = createTextElement(
                'p',
                'network-empty',
                results.status === 'success'
                    ? 'Não foram encontradas redes.'
                    : 'Os resultados da última pesquisa aparecerão aqui.'
            );
            list.appendChild(empty);
            return;
        }

        networks.forEach(network => {
            const card = document.createElement('article');
            card.className = 'wifi-network-card';

            const main = document.createElement('div');
            main.className = 'wifi-network-main';

            const titleRow = document.createElement('div');
            titleRow.className = 'wifi-network-title-row';

            const title = createTextElement(
                'strong',
                'wifi-network-name',
                network.display_ssid || network.ssid || 'Rede oculta'
            );
            titleRow.appendChild(title);

            if (network.open) {
                titleRow.appendChild(
                    createTextElement('span', 'wifi-security-badge open', 'Aberta')
                );
            } else {
                titleRow.appendChild(
                    createTextElement(
                        'span',
                        'wifi-security-badge',
                        network.security || 'Protegida'
                    )
                );
            }

            const details = createTextElement(
                'div',
                'wifi-network-details',
                `${signalLabel(network.signal)} · ${Number(network.signal) || 0}% · canal ${network.channel || '—'}`
            );

            if (Number(network.access_points) > 1) {
                details.textContent += ` · ${network.access_points} pontos de acesso`;
            }

            main.append(titleRow, details);

            const connect = createTextElement('button', 'btn wifi-connect-button', 'Ligar');
            connect.type = 'button';
            connect.disabled = !latestNetworkPayload ||
                latestNetworkPayload.network?.actual?.mode !== 'hotspot';
            connect.addEventListener('click', () => {
                beginNetworkConnection({
                    ssid: String(network.ssid || ''),
                    displayName: String(network.display_ssid || network.ssid || 'Rede oculta'),
                    security: securityForNetwork(network),
                    hidden: Boolean(network.hidden)
                });
            });

            card.append(main, connect);
            list.appendChild(card);
        });
    }

    function renderProfiles(profiles, activeUUID) {
        const list = element('saved-profile-list');
        if (!list) return;
        list.replaceChildren();

        if (!profiles.length) {
            list.appendChild(
                createTextElement(
                    'p',
                    'network-empty',
                    'Ainda não existem redes guardadas pelo PiBook.'
                )
            );
            return;
        }

        profiles.forEach(profile => {
            const row = document.createElement('article');
            row.className = 'saved-profile-card';

            const information = document.createElement('div');
            information.className = 'saved-profile-info';

            const titleLine = document.createElement('div');
            titleLine.className = 'saved-profile-title';

            titleLine.appendChild(
                createTextElement(
                    'strong',
                    '',
                    profile.ssid || profile.name || 'Rede guardada'
                )
            );

            if (profile.uuid === activeUUID || profile.active) {
                titleLine.appendChild(
                    createTextElement('span', 'saved-profile-active', 'Ativa')
                );
            }

            information.appendChild(titleLine);
            information.appendChild(
                createTextElement(
                    'span',
                    'saved-profile-meta',
                    profile.ssid
                        ? 'Perfil guardado pelo PiBook'
                        : `Perfil ${profile.name}`
                )
            );

            const actions = document.createElement('div');
            actions.className = 'saved-profile-actions';

            if (!(profile.uuid === activeUUID || profile.active)) {
                const useButton = createTextElement('button', 'btn btn-secondary', 'Usar');
                useButton.type = 'button';
                useButton.addEventListener('click', () => activateSavedWiFi(profile.uuid));
                actions.appendChild(useButton);
            }

            const forgetButton = createTextElement('button', 'btn btn-danger', 'Esquecer');
            forgetButton.type = 'button';
            forgetButton.addEventListener('click', () => forgetProfile(profile));
            actions.appendChild(forgetButton);

            row.append(information, actions);
            list.appendChild(row);
        });
    }

    function rememberPending(action, details = {}) {
        try {
            localStorage.setItem(
                NETWORK_PENDING_KEY,
                JSON.stringify({
                    action,
                    details,
                    startedAt: Date.now()
                })
            );
        } catch (error) {
            console.warn('Não foi possível guardar o estado da transição.', error);
        }
    }

    function pendingAction() {
        try {
            const raw = localStorage.getItem(NETWORK_PENDING_KEY);
            if (!raw) return null;
            const value = JSON.parse(raw);
            if (!value?.startedAt || Date.now() - value.startedAt > NETWORK_PENDING_MAX_AGE) {
                localStorage.removeItem(NETWORK_PENDING_KEY);
                return null;
            }
            return value;
        } catch (error) {
            localStorage.removeItem(NETWORK_PENDING_KEY);
            return null;
        }
    }

    function clearPendingAction() {
        try {
            localStorage.removeItem(NETWORK_PENDING_KEY);
        } catch (error) {
            // Nothing else is needed.
        }
    }

    function reconcilePendingAction(payload) {
        const pending = pendingAction();
        if (!pending) return;

        const actual = payload?.network?.actual || {};
        const state = payload?.network?.state || {};
        const connectionResult = payload?.connection?.result || {};
        const scanResult = payload?.scan?.results || {};

        if (pending.action === 'scan' && scanResult.status === 'success' && state.operation === 'idle') {
            const generatedAt = scanTimestamp(scanResult);
            if (generatedAt) {
                rememberVisibleWiFiScan(generatedAt);
            }

            clearPendingAction();
            hideTransition();

            const count = Number(scanResult.count) || 0;
            setMessage(
                count === 1
                    ? 'Pesquisa concluída: 1 rede encontrada.'
                    : `Pesquisa concluída: ${count} redes encontradas.`,
                'success'
            );
        } else if (
            pending.action === 'scan' &&
            ['error', 'failed'].includes(scanResult.status) &&
            state.operation === 'idle'
        ) {
            clearPendingAction();
            hideTransition();
            setMessage(
                `A pesquisa falhou: ${scanResult.error || 'erro desconhecido'}`,
                'error'
            );
        } else if (pending.action === 'connect' && ['success', 'failed'].includes(connectionResult.status)) {
            clearPendingAction();
            hideTransition();
            if (connectionResult.status === 'success') {
                setMessage(`Ligação à rede ${connectionResult.ssid || ''} concluída.`, 'success');
            } else {
                setMessage(
                    `Não foi possível ligar a ${connectionResult.ssid || 'essa rede'}. O hotspot foi restaurado.`,
                    'error'
                );
            }
        } else if (pending.action === 'hotspot' && actual.mode === 'hotspot' && state.operation === 'idle') {
            clearPendingAction();
            hideTransition();
            setMessage('Hotspot PiBook ativado.', 'success');
        } else if (pending.action === 'wifi' && actual.mode === 'wifi' && state.operation === 'idle') {
            clearPendingAction();
            hideTransition();
            setMessage('Ligação Wi-Fi restaurada.', 'success');
        } else if (pending.action === 'forget' && state.operation === 'idle') {
            clearPendingAction();
            hideTransition();
            setMessage('Rede esquecida.', 'success');
        }
    }

    function showTransition(action, title, text) {
        rememberPending(action);
        const overlay = element('network-transition-overlay');
        element('network-transition-title').textContent = title;
        element('network-transition-text').textContent = text;
        const address = element('network-transition-address');
        if (address) {
            address.textContent = '';
            address.hidden = true;
        }

        overlay.hidden = false;

        // Enquanto existe uma transição pendente, consultar o estado
        // sequencialmente. Isto é especialmente importante no scan em
        // Wi-Fi, porque a ligação não cai e a página permanece aberta.
        const pollPending = async () => {
            const pending = pendingAction();
            if (!pending || pending.action !== action) return;

            try {
                await refreshNetworkStatus(false, true);
            } catch (error) {
                // Durante mudanças reais de rede a ligação pode cair.
                // Voltamos a tentar enquanto a ação continuar pendente.
            }

            const stillPending = pendingAction();
            if (stillPending && stillPending.action === action) {
                setTimeout(pollPending, 1500);
            }
        };

        setTimeout(pollPending, 1000);
    }

    function hideTransition() {
        const overlay = element('network-transition-overlay');
        if (overlay) overlay.hidden = true;
    }

    async function refreshNetworkStatus(
        showErrors = true,
        force = false
    ) {
        // Reuse the same Promise instead of allowing overlapping requests.
        if (networkStatusRequest) {
            return networkStatusRequest;
        }

        const url = force
            ? '/api/network/status?fresh=1'
            : '/api/network/status';

        networkStatusRequest = (async () => {
            try {
                const payload = await apiRequest(url);
                lastNetworkStatusRefresh = Date.now();

                if (showErrors) setMessage('');
                renderNetworkStatus(payload);

                return payload;
            } catch (error) {
                if (showErrors) {
                    setMessage(
                        `Não foi possível atualizar o estado: ${error.message}`,
                        'error'
                    );
                }
                throw error;
            }
        })();

        try {
            return await networkStatusRequest;
        } finally {
            networkStatusRequest = null;
        }
    }

    async function activateHotspot() {
        if (!confirm('Ativar o hotspot PiBook? A ligação atual será interrompida.')) return;
        setMessage('');
        try {
            await apiRequest('/api/network/hotspot', {
                method: 'POST',
                body: JSON.stringify({})
            });
            showTransition(
                'hotspot',
                'A ativar o hotspot PiBook…',
                'Liga o dispositivo à rede “PiBook” quando ela aparecer. O portal abrirá esta página.'
            );
        } catch (error) {
            setMessage(error.message, 'error');
        }
    }

    async function activateSavedWiFi(uuid = '') {
        if (!confirm('Ligar o PiBook ao Wi-Fi guardado? A ligação ao hotspot será interrompida.')) return;
        setMessage('');
        try {
            await apiRequest('/api/network/wifi', {
                method: 'POST',
                body: JSON.stringify(uuid ? {uuid} : {})
            });
            showTransition(
                'wifi',
                'A restaurar o Wi-Fi…',
                'Volta a ligar o dispositivo à rede externa e abre pibook.local quando o PiBook estiver disponível.'
            );
        } catch (error) {
            setMessage(error.message, 'error');
        }
    }

    async function startWiFiScan() {
        const mode = latestNetworkPayload?.network?.actual?.mode;

        if (
            mode === 'hotspot' &&
            !confirm(
                'Iniciar a pesquisa? O hotspot desaparecerá durante ' +
                'alguns segundos e regressará automaticamente.'
            )
        ) {
            return;
        }

        setMessage('');
        clearVisibleWiFiScan();

        try {
            await apiRequest('/api/network/scan', {
                method: 'POST',
                body: JSON.stringify({})
            });

            if (mode === 'hotspot') {
                showTransition(
                    'scan',
                    'A pesquisar redes Wi-Fi…',
                    'O hotspot desaparecerá temporariamente. ' +
                    'Volta a ligar à rede “PiBook” quando ela reaparecer.'
                );
            } else {
                showTransition(
                    'scan',
                    'A pesquisar redes Wi-Fi…',
                    'A ligação Wi-Fi atual será mantida durante a pesquisa.'
                );
            }
        } catch (error) {
            setMessage(error.message, 'error');
        }
    }

    function openConnectionModal(network) {
        element('wifi-connect-ssid').value = network.ssid;
        element('wifi-connect-security').value = network.security;
        element('wifi-connect-hidden').value = network.hidden ? 'true' : 'false';
        element('wifi-connect-password').value = '';
        element('wifi-connect-modal-description').textContent =
            `Introduz a palavra-passe de “${network.displayName}”.`;
        element('wifi-connect-modal').hidden = false;
        setTimeout(() => element('wifi-connect-password').focus(), 0);
    }

    function closeConnectionModal() {
        const modal = element('wifi-connect-modal');
        if (!modal) return;
        modal.hidden = true;
        element('wifi-connect-password').value = '';
    }

    function beginNetworkConnection(network) {
        if (!network.ssid && !network.hidden) {
            setMessage('O nome desta rede é inválido.', 'error');
            return;
        }

        if (network.security === 'open') {
            if (confirm(`Ligar à rede aberta “${network.displayName}”?`)) {
                connectToNetwork({
                    ssid: network.ssid,
                    password: '',
                    security: 'open',
                    hidden: network.hidden
                });
            }
            return;
        }

        openConnectionModal(network);
    }

    async function connectToNetwork(payload) {
        setMessage('');
        try {
            await apiRequest('/api/network/connect', {
                method: 'POST',
                body: JSON.stringify(payload)
            });

            closeConnectionModal();
            showTransition(
                'connect',
                `A ligar a “${payload.ssid}”…`,
                'Se a ligação funcionar, volta à rede escolhida e abre pibook.local. Se falhar, o hotspot PiBook regressará.'
            );
        } catch (error) {
            hideTransition();
            setMessage(error.message, 'error');
        } finally {
            const password = element('wifi-connect-password');
            const manualPassword = element('manual-password');
            if (password) password.value = '';
            if (manualPassword) manualPassword.value = '';
        }
    }

    async function forgetProfile(profile) {
        const name = profile.ssid || profile.name || 'esta rede';
        if (!confirm(`Esquecer “${name}”?`)) return;

        try {
            await apiRequest(`/api/network/profiles/${encodeURIComponent(profile.uuid)}`, {
                method: 'DELETE'
            });

            const isActive = Boolean(profile.active);
            if (isActive) {
                showTransition(
                    'forget',
                    'A esquecer a rede ativa…',
                    'O PiBook vai ativar o hotspot antes de eliminar o perfil.'
                );
            } else {
                rememberPending('forget');
                setMessage('A rede está a ser removida…', 'info');
                setTimeout(() => refreshNetworkStatus(), 1800);
            }
        } catch (error) {
            setMessage(error.message, 'error');
        }
    }

    function updateManualSecurity() {
        const security = element('manual-security').value;
        const group = element('manual-password-group');
        const password = element('manual-password');
        const open = security === 'open';

        group.hidden = open;
        password.required = !open;
        if (open) password.value = '';
    }

    function togglePassword(button) {
        const target = element(button.dataset.passwordTarget);
        if (!target) return;
        const showing = target.type === 'text';
        target.type = showing ? 'password' : 'text';
        button.textContent = showing ? 'Mostrar' : 'Ocultar';
    }

    function bindNetworkEvents() {
        element('network-refresh-btn')?.addEventListener('click', () => refreshNetworkStatus(true, true));
        element('network-hotspot-btn')?.addEventListener('click', activateHotspot);
        element('network-wifi-btn')?.addEventListener('click', () => activateSavedWiFi());
        element('wifi-scan-btn')?.addEventListener('click', startWiFiScan);
        element('manual-security')?.addEventListener('change', updateManualSecurity);

        element('manual-network-form')?.addEventListener('submit', event => {
            event.preventDefault();
            const ssid = element('manual-ssid').value.trim();
            const security = element('manual-security').value;
            const hidden = element('manual-hidden').checked;
            const password = security === 'open' ? '' : element('manual-password').value;

            connectToNetwork({ssid, password, security, hidden});
        });

        element('wifi-connect-form')?.addEventListener('submit', event => {
            event.preventDefault();
            connectToNetwork({
                ssid: element('wifi-connect-ssid').value,
                password: element('wifi-connect-password').value,
                security: element('wifi-connect-security').value,
                hidden: element('wifi-connect-hidden').value === 'true'
            });
        });

        element('wifi-connect-cancel')?.addEventListener('click', closeConnectionModal);
        element('wifi-connect-modal-close')?.addEventListener('click', closeConnectionModal);
        document.querySelector('[data-close-network-modal="true"]')
            ?.addEventListener('click', closeConnectionModal);

        document.querySelectorAll('.network-password-toggle').forEach(button => {
            button.addEventListener('click', () => togglePassword(button));
        });

        document.addEventListener('keydown', event => {
            if (event.key === 'Escape' && !element('wifi-connect-modal')?.hidden) {
                closeConnectionModal();
            }
        });
    }

    function bindNetworkCollapsiblePanels() {
        document.querySelectorAll('[data-network-toggle]').forEach(button => {
            if (button.dataset.networkToggleBound === 'true') return;

            const targetId = button.dataset.networkToggle;
            const target = element(targetId);
            if (!target) return;

            button.dataset.networkToggleBound = 'true';

            button.addEventListener('click', () => {
                const opening = target.hidden;

                target.hidden = !opening;
                button.setAttribute('aria-expanded', String(opening));
                button.textContent = opening ? 'Ocultar' : 'Mostrar';
            });
        });
    }

    function initNetworkPage() {
        bindNetworkCollapsiblePanels();

        if (!networkPageInitialized) {
            bindNetworkEvents();
            updateManualSecurity();
            networkPageInitialized = true;
        }

        const refreshAge = Date.now() - lastNetworkStatusRefresh;
        const forceRefresh = Boolean(pendingAction());

        if (
            !networkStatusRequest &&
            (forceRefresh || refreshAge > 15000)
        ) {
            refreshNetworkStatus(true, forceRefresh).catch(() => {});
        }

        if (networkStatusTimer) clearInterval(networkStatusTimer);
        networkStatusTimer = setInterval(() => {
            const pageVisible = document.visibilityState === 'visible';
            const overlayHidden =
                element('network-transition-overlay')?.hidden !== false;

            if (
                pageVisible &&
                isNetworkSectionActive() &&
                overlayHidden &&
                !networkStatusRequest
            ) {
                refreshNetworkStatus(false, false).catch(() => {});
            }
        }, 120000);
    }

    // Existing local IP scanner, retained below the Wi-Fi controls.
    function initLocalIPScanner() {
        fetch('/api/ipscanner/status', {cache: 'no-store'})
            .then(response => response.json())
            .then(data => {
                if (data.local_ip) {
                    element('local-ip').textContent = data.network
                        ? `PiBook: ${data.local_ip} · Rede: ${data.network}`
                        : `PiBook: ${data.local_ip}`;
                } else {
                    element('local-ip').textContent =
                        'Endereço local indisponível';
                }
                updateLocalScanUI(data);
            })
            .catch(error => {
                element('local-ip').textContent =
                    `Não foi possível consultar o IP Scanner: ${error.message}`;
            });
    }

    window.startNetworkScan = function startNetworkScan() {
        const button = element('scan-btn');
        const text = element('scan-btn-text');
        button.disabled = true;
        text.textContent = 'A pesquisar…';

        const statusBox = element('scan-progress-container');
        const statusText = element('scan-status');

        statusBox.hidden = false;
        statusBox.classList.remove('is-success', 'is-error');
        statusText.textContent = 'A pesquisar dispositivos…';

        const deviceHeading = document.querySelector('.network-device-heading');
        const deviceList = element('device-list');

        if (deviceHeading) deviceHeading.hidden = true;
        if (deviceList) deviceList.hidden = true;

        element('device-count').textContent = '0';

        fetch('/api/ipscanner/scan', {method: 'POST'})
            .then(response => response.json())
            .then(data => {
                if (data.started || data.status === 'already_scanning') {
                    startLocalIPPolling();
                } else {
                    throw new Error(data.error || 'Não foi possível iniciar a pesquisa.');
                }
            })
            .catch(error => {
                button.disabled = false;
                text.textContent = 'Pesquisar dispositivos';
                setMessage(`IP Scanner: ${error.message}`, 'error');
            });
    };

    function startLocalIPPolling() {
        if (localIPScanInterval) {
            clearTimeout(localIPScanInterval);
            localIPScanInterval = null;
        }

        const poll = () => {
            localIPScanInterval = null;

            fetch('/api/ipscanner/status', {cache: 'no-store'})
                .then(response => response.json())
                .then(data => {
                    updateLocalScanUI(data);

                    if (data.scanning) {
                        localIPScanInterval = setTimeout(poll, 2000);
                    } else {
                        stopLocalIPPolling();
                    }
                })
                .catch(error => {
                    console.error('Erro no IP Scanner:', error);
                    localIPScanInterval = setTimeout(poll, 3000);
                });
        };

        localIPScanInterval = setTimeout(poll, 500);
    }

    function stopLocalIPPolling() {
        if (localIPScanInterval) {
            clearTimeout(localIPScanInterval);
            localIPScanInterval = null;
        }

        element('scan-btn').disabled = false;
        element('scan-btn-text').textContent = 'Pesquisar dispositivos';
    }

    function updateLocalScanUI(data) {
        const devices = Array.isArray(data.devices) ? data.devices : [];
        const scanning = Boolean(data.scanning);
        const status = String(data.status || '');
        const error = String(data.error || '').trim();

        const statusBox = element('scan-progress-container');
        const statusText = element('scan-status');

        if (scanning) {
            statusBox.hidden = false;
            statusBox.classList.remove('is-success', 'is-error');
            statusText.textContent = 'A pesquisar dispositivos…';
        } else if (status === 'success') {
            statusBox.hidden = true;
            statusBox.classList.remove('is-success', 'is-error');
        } else if (status === 'error' || error) {
            statusBox.hidden = false;
            statusBox.classList.remove('is-success');
            statusBox.classList.add('is-error');
            statusText.textContent =
                error || 'Não foi possível concluir a pesquisa.';
        } else {
            statusBox.hidden = true;
            statusBox.classList.remove('is-success', 'is-error');
        }

        const deviceHeading = document.querySelector('.network-device-heading');
        const container = element('device-list');

        if (deviceHeading) deviceHeading.hidden = scanning;
        if (container) container.hidden = scanning;

        element('device-count').textContent = String(devices.length);

        if (!devices.length) {
            container.replaceChildren(
                createTextElement(
                    'p',
                    'network-empty',
                    scanning
                        ? 'A procurar equipamentos na rede local…'
                        : (status === 'stale'
                            ? 'Pesquisa necessária nesta rede.'
                            : (status === 'unavailable'
                                ? 'Liga o PiBook a uma rede para pesquisar dispositivos.'
                                : (status === 'never'
                                    ? 'Seleciona “Pesquisar dispositivos” para iniciar.'
                                    : 'Não foram encontrados dispositivos.')))
                )
            );
        } else {
            const list = document.createElement('div');
            list.className = 'network-device-list';

            devices.forEach(device => {
                const card = document.createElement('article');
                card.className = 'network-device-card';

                const ip = createTextElement(
                    'strong',
                    'network-device-ip network-monospace',
                    device.ip || 'Endereço desconhecido'
                );
                card.appendChild(ip);

                if (device.mac) {
                    const macRow = document.createElement('div');
                    macRow.className = 'network-device-detail';
                    macRow.appendChild(
                        createTextElement('span', 'network-device-label', 'MAC')
                    );
                    macRow.appendChild(
                        createTextElement(
                            'span',
                            'network-device-value network-monospace',
                            device.mac
                        )
                    );
                    card.appendChild(macRow);
                }

                let vendor = String(device.name || '').trim();
                if (
                    vendor.toLowerCase().startsWith('(unknown') ||
                    vendor.toLowerCase() === 'unknown'
                ) {
                    vendor = '';
                }

                if (vendor) {
                    const vendorRow = document.createElement('div');
                    vendorRow.className = 'network-device-detail';
                    vendorRow.appendChild(
                        createTextElement('span', 'network-device-label', 'Fabricante')
                    );
                    vendorRow.appendChild(
                        createTextElement('span', 'network-device-value', vendor)
                    );
                    card.appendChild(vendorRow);
                }

                list.appendChild(card);
            });

            container.replaceChildren(list);
        }

        element('scan-btn').disabled = scanning;
        element('scan-btn-text').textContent = scanning
            ? 'A pesquisar…'
            : 'Pesquisar dispositivos';
    }

    // main.js already calls this name when the existing section is opened.
    window.initIPScanner = function initIPScanner() {
        initNetworkPage();
        initLocalIPScanner();
    };
})();
