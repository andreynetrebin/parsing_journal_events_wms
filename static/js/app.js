// static/js/app.js
// Дашборд анализа ТСД (WMS) - вся логика приложения

// ===== Глобальные переменные =====
let pairsDT, slowDT, orphansDT, topDT, statsDT, errorsDT, errorStatsDT;
let hourlyChart = null;
let dateRangePicker = null;
let actionsByMethod = {};
let methodSelect, deviceSelect, actionSelect;

// ===== Даты по умолчанию: последние 24 часа (локальное время) =====
function getDefaultDateRange() {
    const now = new Date();
    const Last24HoursAgo = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    return {
        start: formatDateTimeLocal(Last24HoursAgo),
        end: formatDateTimeLocal(now)
    };
}

// ===== Объект фильтров =====
const defaultRange = getDefaultDateRange();
let currentFilters = {
    method: [], device: [], action: [], threshold: 0, status: '',
    startDate: defaultRange.start, endDate: defaultRange.end,
    requestId: '', bodySearch: ''
};

// ===== Утилиты =====
function formatDate(d) {
    if (!d || d === 'N/A') return '-';
    return String(d).replace('T', ' ').substring(0, 19);
}

function statusBadge(s) {
    if (s === 'ok') return '<span class="badge bg-success">OK</span>';
    if (s === 'no_response') return '<span class="badge bg-warning text-dark">Нет ответа</span>';
    if (s === 'no_request') return '<span class="badge bg-danger">Нет запроса</span>';
    return `<span class="badge bg-secondary">${s||'-'}</span>`;
}

function formatDateLocal(dateObj) {
    if (!dateObj) return '';
    const year = dateObj.getFullYear();
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    return `${year}-${month}-${day}`;
}

function formatDateTimeLocal(dateObj) {
    if (!dateObj) return '';
    const year = dateObj.getFullYear();
    const month = String(dateObj.getMonth() + 1).padStart(2, '0');
    const day = String(dateObj.getDate()).padStart(2, '0');
    const hours = String(dateObj.getHours()).padStart(2, '0');
    const minutes = String(dateObj.getMinutes()).padStart(2, '0');
    const seconds = String(dateObj.getSeconds()).padStart(2, '0');
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}`;
}

function escapeHtml(t) {
    if (!t) return '';
    const map = {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'};
    return String(t).replace(/[&<>"']/g, m => map[m]);
}

function formatJson(s) {
    if (!s) return 'Нет данных';
    try { return JSON.stringify(JSON.parse(s), null, 2); } catch (e) { return s; }
}

function escapeAttr(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/\n/g, '&#10;')
        .replace(/\r/g, '&#13;');
}

// ===== Быстрый выбор интервала дат =====
function setDatePreset(preset) {
    const now = new Date();
    let start = new Date(now);
    let end = new Date(now);

    if (preset === 1) {
        start.setHours(now.getHours() - 1);
    } else if (preset === 24) {
        start.setDate(now.getDate() - 1);
    } else if (preset === 'today') {
        start.setHours(0, 0, 0, 0);
    } else if (preset === 'week') {
        start.setDate(now.getDate() - 7);
    }

    const startStr = formatDateTimeLocal(start);
    const endStr = formatDateTimeLocal(end);

    dateRangePicker.setDate([startStr, endStr]);
    currentFilters.startDate = startStr;
    currentFilters.endDate = endStr;

    applyFilters();
}

// ===== Сброс фильтров =====
function resetFilters() {
    document.getElementById('filter-threshold').value = '0';
    document.getElementById('filter-status').value = '';
    document.getElementById('filter-request-id').value = '';
    document.getElementById('filter-body-search').value = '';

    const def = getDefaultDateRange();
    if (dateRangePicker) dateRangePicker.setDate([def.start, def.end]);

    if (methodSelect) methodSelect.clear();
    if (deviceSelect) deviceSelect.clear();
    if (actionSelect) actionSelect.clear();
    currentFilters.action = [];
    currentFilters.bodySearch = '';

    currentFilters = {
        method: [], device: [], threshold: 0, status: '',
        startDate: def.start, endDate: def.end, requestId: '', bodySearch: ''
    };
    document.getElementById('slow-threshold-label').textContent = '0';
    loadAll();
}

// ===== ГЛОБАЛЬНАЯ функция обновления Action =====
function refreshActionOptions() {
    if (!methodSelect || !actionSelect) return;

    let selectedMethods = methodSelect.getValue();
    if (typeof selectedMethods === 'string') {
        selectedMethods = selectedMethods ? [selectedMethods] : [];
    }

    let prev = actionSelect.getValue();
    prev = Array.isArray(prev) ? prev : (prev ? [prev] : []);

    actionSelect.clear();
    actionSelect.clearOptions();

    if (!selectedMethods || selectedMethods.length === 0) {
        actionSelect.settings.placeholder = 'Сначала выберите метод...';
        actionSelect.updatePlaceholder();
        return;
    }

    const union = new Set();
    selectedMethods.forEach(m => {
        (actionsByMethod[m] || []).forEach(a => union.add(a));
    });
    const actions = Array.from(union).sort();

    if (actions.length > 0) {
        actions.forEach(a => actionSelect.addOption({ value: a, text: a }));
        actionSelect.settings.placeholder = `Выберите Action (${actions.length})...`;
    } else {
        actionSelect.settings.placeholder = 'Нет Action для выбранных методов';
    }

    prev.filter(v => union.has(v)).forEach(v => actionSelect.addItem(v, false));
    actionSelect.updatePlaceholder();
    actionSelect.refreshOptions(false);
    console.log("✅ Action обновлён:", actions.length, "значений");
}

// ===== Сброс UI поиска =====
function resetSearchUI() {
    const searchBtn = document.querySelector('button[onclick="applyFilters()"]');
    const statusMsg = document.getElementById('search-status-msg');

    if (searchBtn) {
        searchBtn.innerHTML = '🔍';
        searchBtn.disabled = false;
    }
    if (statusMsg) {
        statusMsg.classList.add('d-none');
    }
}

// ===== Применение фильтров =====
function applyFilters() {
    let mVal = methodSelect ? methodSelect.getValue() : [];
    let dVal = deviceSelect ? deviceSelect.getValue() : [];
    let aVal = actionSelect ? actionSelect.getValue() : [];

    currentFilters = {
        method: Array.isArray(mVal) ? mVal : (mVal ? [mVal] : []),
        device: Array.isArray(dVal) ? dVal : (dVal ? [dVal] : []),
        action: Array.isArray(aVal) ? aVal : (aVal ? [aVal] : []),
        threshold: parseFloat(document.getElementById('filter-threshold').value) || 0,
        status: document.getElementById('filter-status').value,
        startDate: currentFilters.startDate,
        endDate: currentFilters.endDate,
        requestId: document.getElementById('filter-request-id').value,
        bodySearch: document.getElementById('filter-body-search').value.trim()
    };

    const searchBtn = document.querySelector('button[onclick="applyFilters()"]');
    const statusMsg = document.getElementById('search-status-msg');
    const searchTermDisplay = document.getElementById('search-term-display');

    if (currentFilters.bodySearch) {
        if (searchBtn) {
            searchBtn.innerHTML = '⏳ Поиск...';
            searchBtn.disabled = true;
        }
        if (statusMsg && searchTermDisplay) {
            searchTermDisplay.textContent = `"${currentFilters.bodySearch}"`;
            statusMsg.classList.remove('d-none');
        }
    } else {
        resetSearchUI();
    }

    console.log("🔍 Применяем фильтры:", currentFilters);
    document.getElementById('slow-threshold-label').textContent = currentFilters.threshold;

    loadAll();
}

// ===== Экспорт данных =====
function startExport() {
    const format = document.querySelector('input[name="exportFormat"]:checked').value;
    const limit = document.getElementById('export-limit').value;

    const params = new URLSearchParams({
        format: format,
        limit: limit
    });

    if (currentFilters.method && currentFilters.method.length > 0) {
        currentFilters.method.forEach(m => params.append('method', m));
    }
    if (currentFilters.device && currentFilters.device.length > 0) {
        currentFilters.device.forEach(d => params.append('device', d));
    }
    if (currentFilters.status) {
        params.set('pair_status', currentFilters.status);
    }
    if (currentFilters.threshold > 0) {
        params.set('min_ms', Math.round(currentFilters.threshold * 1000));
    }
    if (currentFilters.startDate) {
        params.set('start_date', currentFilters.startDate);
    }
    if (currentFilters.endDate) {
        params.set('end_date', currentFilters.endDate);
    }
    if (currentFilters.bodySearch) {
        params.set('body_search', currentFilters.bodySearch);
    }

    const url = `/api/export?${params.toString()}`;

    const modal = bootstrap.Modal.getInstance(document.getElementById('exportModal'));
    if (modal) modal.hide();

    showExportNotification(`⏳ Начинается выгрузка в формате ${format.toUpperCase()}...`);

    const link = document.createElement('a');
    link.href = url;
    link.style.display = 'none';
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);

    setTimeout(() => {
        showExportNotification(`✅ Файл сохранён. Проверьте папку "Загрузки".`);
    }, 2000);
}

function showExportNotification(message) {
    const old = document.getElementById('export-notification');
    if (old) old.remove();

    const div = document.createElement('div');
    div.id = 'export-notification';
    div.className = 'alert alert-success position-fixed';
    div.style.cssText = 'top: 80px; right: 20px; z-index: 9999; min-width: 300px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);';
    div.innerHTML = `<i class="bi bi-check-circle-fill me-2"></i>${message}`;
    document.body.appendChild(div);

    setTimeout(() => {
        div.style.transition = 'opacity 0.5s';
        div.style.opacity = '0';
        setTimeout(() => div.remove(), 500);
    }, 4000);
}

document.getElementById('exportModal').addEventListener('show.bs.modal', function () {
    const filtersDiv = document.getElementById('export-active-filters');
    const parts = [];

    if (currentFilters.startDate && currentFilters.endDate) {
        parts.push(`📅 Период: ${currentFilters.startDate} — ${currentFilters.endDate}`);
    }
    if (currentFilters.method && currentFilters.method.length > 0) {
        parts.push(`🔧 Методы: ${currentFilters.method.join(', ')}`);
    }
    if (currentFilters.device && currentFilters.device.length > 0) {
        parts.push(`📱 ТСД: ${currentFilters.device.join(', ')}`);
    }
    if (currentFilters.status) {
        const statusNames = {
            'ok': 'Успешно',
            'no_response': 'Без ответа',
            'no_request': 'Без запроса'
        };
        parts.push(`📊 Статус: ${statusNames[currentFilters.status] || currentFilters.status}`);
    }
    if (currentFilters.threshold > 0) {
        parts.push(`⏱ Медленнее ${currentFilters.threshold} сек`);
    }
    if (currentFilters.requestId) {
        parts.push(`🆔 ID запроса: ${currentFilters.requestId}`);
    }

    if (parts.length === 0) {
        filtersDiv.innerHTML = '<span class="text-muted">Без фильтров (все данные за период)</span>';
    } else {
        filtersDiv.innerHTML = parts.map(p => `<div>${p}</div>`).join('');
    }
});

// ===== Загрузка всех данных =====
function loadAll() {
    loadSummary();
    loadPairs();
    loadSlow();
    loadOrphans();
    loadTop();
    loadMinutely();
    loadStats();
    loadErrorStats();
}

// ===== Сводка =====
async function loadSummary() {
    try {
        const r = await fetch('/api/dashboard/summary');
        const d = await r.json();
        document.getElementById('stat-total').textContent = d.total_pairs?.toLocaleString() || 0;
        document.getElementById('stat-ok').textContent = d.ok_pairs?.toLocaleString() || 0;
        document.getElementById('stat-no-resp').textContent = d.no_response?.toLocaleString() || 0;
        document.getElementById('stat-no-req').textContent = d.no_request?.toLocaleString() || 0;
        document.getElementById('stat-avg').textContent = d.avg_ms?.toFixed(1) || '-';
        document.getElementById('stat-p95').textContent = d.p95_ms?.toFixed(0) || '-';
    } catch (e) { console.error(e); }
}

// ===== Загрузка фильтров (методы, устройства, действия) =====
async function loadFilters() {
    console.log("🚀 ШАГ 0: loadFilters начала выполнение");

    const elMethod = document.getElementById('filter-method');
    const elAction = document.getElementById('filter-action');
    const elDevice = document.getElementById('filter-device');

    if (!elMethod || !elAction || !elDevice) {
        console.error("❌ Один из select не найден в HTML!", elMethod, elAction, elDevice);
        return;
    }

    try {
        console.log("🚀 ШАГ 1: Загрузка методов, устройств и карты действий...");
        const [methods, devices, actionsData] = await Promise.all([
            fetch('/api/methods').then(r => r.json()),
            fetch('/api/devices').then(r => r.json()),
            fetch('/api/actions').then(r => r.json())
        ]);

        actionsByMethod = (actionsData && typeof actionsData === 'object' && !Array.isArray(actionsData))
            ? actionsData : {};

        console.log("✅ Методов:", methods.length,
                    "| Устройств:", devices.length,
                    "| Методов с Action в карте:", Object.keys(actionsByMethod).length);

        const importantMethods = new Set([
            'InventorySKU', 'GetGeneralDatas', 'MovementLocation',
            'SortByPickingSKU', 'ExpectedReceipt', 'InventoryLocation',
            'PickingSKU', 'PutTaskQueue'
        ]);

        methodSelect = new TomSelect('#filter-method', {
            plugins: ['remove_button'],
            maxItems: null,
            render: {
                option: function(data, escape) {
                    const isImportant = importantMethods.has(data.value);
                    return `<div class="option" style="${isImportant ? 'font-weight:bold; color:#0d6efd;' : ''}">
                            ${isImportant ? '🔹 ' : ''}${escape(data.text)}</div>`;
                },
                item: function(data, escape) {
                    const isImportant = importantMethods.has(data.value);
                    return `<div class="item" style="${isImportant ? 'font-weight:bold;' : ''}">${escape(data.text)}</div>`;
                }
            }
        });
        methods.forEach(m => methodSelect.addOption({value: m, text: m}));

        actionSelect = new TomSelect('#filter-action', {
            plugins: ['remove_button'],
            maxItems: null,
            placeholder: 'Сначала выберите метод...',
            options: []
        });

        deviceSelect = new TomSelect('#filter-device', {
            plugins: ['remove_button'],
            maxItems: null
        });
        devices.forEach(d => deviceSelect.addOption({value: d, text: d}));

        methodSelect.on('change', refreshActionOptions);
        methodSelect.on('item_add', refreshActionOptions);
        methodSelect.on('item_remove', refreshActionOptions);

        console.log("🎉 ШАГ 2: loadFilters завершена успешно!");

    } catch (e) {
        console.error("💥 КРИТИЧЕСКАЯ ОШИБКА в loadFilters:", e);
    }
}

// ===== Параметры фильтрации =====
function buildFilterParams(extra = {}) {
    const params = new URLSearchParams(extra);

    if (currentFilters.method && currentFilters.method.length > 0) {
        currentFilters.method.forEach(m => params.append('method', m));
    }
    if (currentFilters.action && currentFilters.action.length > 0) {
        currentFilters.action.forEach(a => params.append('action', a));
    }
    if (currentFilters.device && currentFilters.device.length > 0) {
        currentFilters.device.forEach(d => params.append('device', d));
    }
    if (currentFilters.status) params.set('pair_status', currentFilters.status);
    if (currentFilters.threshold > 0 && (!currentFilters.status || currentFilters.status === 'ok')) {
        params.set('min_ms', Math.round(currentFilters.threshold * 1000));
    }
    if (currentFilters.requestId) params.set('request_id', currentFilters.requestId);
    if (currentFilters.startDate) params.set('start_date', currentFilters.startDate);
    if (currentFilters.endDate) params.set('end_date', currentFilters.endDate);
    if (currentFilters.bodySearch) params.set('body_search', currentFilters.bodySearch);

    return params;
}

function buildCommonParams(extra = {}) {
    const params = new URLSearchParams(extra);
    if (currentFilters.method && currentFilters.method.length > 0) {
        currentFilters.method.forEach(m => params.append('method', m));
    }
    if (currentFilters.action && currentFilters.action.length > 0) {
        currentFilters.action.forEach(a => params.append('action', a));
    }
    if (currentFilters.device && currentFilters.device.length > 0) {
        currentFilters.device.forEach(d => params.append('device', d));
    }
    if (currentFilters.startDate) params.set('start_date', currentFilters.startDate);
    if (currentFilters.endDate) params.set('end_date', currentFilters.endDate);
    if (currentFilters.bodySearch) params.set('body_search', currentFilters.bodySearch);
    return params;
}

// ===== Инициализация DataTables: Таблица событий =====
function initPairsTable() {
    if (pairsDT) pairsDT.destroy();

    pairsDT = $('#pairsTable').DataTable({
        processing: true,
        serverSide: true,
        ajax: function(data, callback) {
            const params = buildFilterParams({
                start: data.start, length: data.length, search: data.search.value || '',
                order_column: data.order[0]?.column ?? 0,
                order_dir: data.order[0]?.dir || 'desc'
            });

            fetch(`/api/pairs/server?${params.toString()}`)
                .then(r => r.json())
                .then(json => {
                    setTimeout(() => resetSearchUI(), 100);
                    $('#pairsTable tbody tr').removeClass('selected');

                    callback({
                        draw: data.draw, recordsTotal: json.recordsTotal, recordsFiltered: json.recordsFiltered,
                        data: json.data.map(r => [
                            formatDate(r.request_ts),
                            formatDate(r.response_ts),
                            r.method,
                            r.action || '-',
                            r.device,
                            r.processing_ms ?? '-',
                            r.request_id
                        ])
                    });
                })
                .catch(err => {
                    setTimeout(() => resetSearchUI(), 100);
                    console.error('Ошибка загрузки пар:', err);
                    callback({ draw: data.draw, recordsTotal: 0, recordsFiltered: 0, data: [] });
                });
        },
        columns: [
            { title: 'Время запроса' },
            { title: 'Время ответа' },
            { title: 'Метод' },
            { title: 'Action' },
            { title: 'ТСД' },
            { title: 'Длит. (мс)' },
            { title: 'ID запроса' }
        ],
        order: [[0, 'desc']],
        language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
        pageLength: 25, lengthMenu: [25, 50, 100, 250, 500], stateSave: false
    });
}

function loadPairs() {
    if (pairsDT) {
        pairsDT.ajax.reload();
    } else {
        initPairsTable();
    }
}

// ===== Инициализация DataTables: Ошибки ТСД =====
function initErrorsTable() {
    if (errorsDT) {
        errorsDT.destroy();
        errorsDT = null;
    }

    console.log('🚀 Инициализация таблицы ошибок...');

    errorsDT = $('#errorsTable').DataTable({
        processing: true,
        serverSide: true,
        deferRender: true,
        autoWidth: false,
        ajax: function(data, callback) {
            const params = buildFilterParams({
                start: data.start,
                length: data.length,
                search: data.search.value || '',
                order_column: data.order[0]?.column ?? 0,
                order_dir: data.order[0]?.dir || 'desc'
            });

            const url = `/api/dashboard/errors/server?${params.toString()}`;
            console.log('📡 Запрос к API ошибок:', url);

            fetch(url)
                .then(r => {
                    if (!r.ok) throw new Error(`HTTP ${r.status}: ${r.statusText}`);
                    return r.json();
                })
                .then(json => {
                    console.log('✅ Получено записей об ошибках:', json.recordsTotal);
                    callback({
                        draw: data.draw,
                        recordsTotal: json.recordsTotal,
                        recordsFiltered: json.recordsFiltered,
                        data: json.data.map(r => [
                            formatDate(r.event_ts),
                            r.method,
                            r.device,
                            r.terminal || '-',
                            r.user_name || '-',
                            {
                                display: `<div class="text-truncate" style="max-width: 300px;" title="${escapeAttr(r.comment_raw || '')}">${escapeHtml((r.comment_raw || '').substring(0, 100))}...</div>`,
                                filter: r.comment_raw || ''
                            }
                        ])
                    });
                })
                .catch(err => {
                    console.error('❌ Ошибка загрузки ошибок:', err);
                    callback({ draw: data.draw, recordsTotal: 0, recordsFiltered: 0, data: [] });
                });
        },
        columns: [
            { title: 'Время' },
            { title: 'Метод' },
            { title: 'ТСД' },
            { title: 'Терминал' },
            { title: 'Пользователь' },
            {
                title: 'Текст ошибки',
                render: function(data, type, row) {
                    if (type === 'display') {
                        return data.display;
                    }
                    return data.filter || '';
                }
            }
        ],
        order: [[0, 'desc']],
        language: {
            url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json",
            emptyTable: "Нет данных об ошибках за выбранный период",
            info: "Показано _START_ - _END_ из _TOTAL_ ошибок",
            infoEmpty: "0 ошибок",
            infoFiltered: "(отфильтровано из _MAX_)"
        },
        pageLength: 25,
        lengthMenu: [25, 50, 100, 250]
    });
}

function loadErrors() {
    if (errorsDT) {
        errorsDT.ajax.reload();
    } else {
        initErrorsTable();
    }
}

// ===== Показ деталей ошибки в модалке =====
function showErrorDetails(errorText) {
    document.getElementById('modal-request-id').textContent = 'Ошибка ТСД';
    document.getElementById('modal-request-id-display').textContent = 'Детали ошибки';
    document.getElementById('modal-method').textContent = '-';
    document.getElementById('modal-device').textContent = '-';
    document.getElementById('modal-user').textContent = '-';
    document.getElementById('modal-session').textContent = '-';
    document.getElementById('modal-terminal').textContent = '-';
    document.getElementById('modal-connection').textContent = '-';
    document.getElementById('modal-req-time').textContent = '';
    document.getElementById('modal-resp-time').textContent = '';
    document.getElementById('modal-processing').textContent = '';

    document.getElementById('modal-request-body').innerHTML = `<pre class="text-danger">${escapeHtml(errorText)}</pre>`;
    document.getElementById('modal-response-body').innerHTML = '<pre class="text-muted">Отсутствует (событие ошибки)</pre>';

    const modal = new bootstrap.Modal(document.getElementById('detailModal'));
    modal.show();
}

// ===== Показ деталей события в правой панели =====
async function showDetails(requestId, device, eventTs) {
    // Подсветка выбранной строки
    $('#pairsTable tbody tr').removeClass('selected');

    // Показываем панель
    const panel = document.getElementById('detail-panel');
    panel.style.display = 'block';

    // Сброс полей
    document.getElementById('panel-request-id').textContent = requestId;
    ['panel-method','panel-device','panel-user','panel-session','panel-terminal','panel-connection','panel-action'].forEach(id => {
        document.getElementById(id).textContent = '-';
    });
    ['panel-req-time','panel-resp-time','panel-processing'].forEach(id => {
        document.getElementById(id).textContent = '';
    });
    document.getElementById('panel-request-body').innerHTML = '<pre>Загрузка...</pre>';
    document.getElementById('panel-response-body').innerHTML = '<pre>Загрузка...</pre>';

    try {
        let url = `/api/events/${requestId}`;
        const qs = new URLSearchParams();
        if (device) qs.set('device', device);
        if (eventTs) qs.set('event_ts', eventTs);
        if (qs.toString()) url += `?${qs.toString()}`;

        const d = await fetch(url).then(r => r.json());
        const evt = d.request || d.response;

        if (evt) {
            document.getElementById('panel-method').textContent = evt.method || '-';
            document.getElementById('panel-device').textContent = evt.device || '-';
            document.getElementById('panel-user').textContent = evt.user_name || '-';
            document.getElementById('panel-session').textContent = evt.session_id || '-';
            document.getElementById('panel-terminal').textContent = evt.terminal || '-';
            document.getElementById('panel-connection').textContent = evt.connection_id || '-';
            document.getElementById('panel-action').textContent = d.action || evt.action || '-';
        }

        if (d.request) {
            document.getElementById('panel-req-time').textContent = formatDate(d.request.event_ts);
            document.getElementById('panel-request-body').innerHTML = `<pre>${escapeHtml(formatJson(d.request.body_json))}</pre>`;
        } else {
            document.getElementById('panel-request-body').innerHTML = '<pre class="text-muted">Запрос не найден</pre>';
        }

        if (d.response) {
            document.getElementById('panel-resp-time').textContent = formatDate(d.response.event_ts);
            document.getElementById('panel-processing').textContent = d.response.processing_ms ? `${d.response.processing_ms} мс` : '';
            document.getElementById('panel-response-body').innerHTML = `<pre>${escapeHtml(formatJson(d.response.body_json))}</pre>`;
        } else {
            document.getElementById('panel-response-body').innerHTML = '<pre class="text-muted">Ответ не найден</pre>';
        }
    } catch (e) {
        console.error(e);
        document.getElementById('panel-request-body').innerHTML = `<pre class="text-danger">Ошибка: ${e.message}</pre>`;
    }
}

function closeDetailPanel() {
    document.getElementById('detail-panel').style.display = 'none';
    $('#pairsTable tbody tr').removeClass('selected');
}

// ===== Медленные запросы =====
async function loadSlow() {
    document.getElementById('slow-body').innerHTML = '<tr><td colspan="8" class="loading">Загрузка...</td></tr>';
    try {
        const params = buildFilterParams({ threshold_ms: Math.max(1, Math.round(currentFilters.threshold * 1000)), limit: 500 });
        const data = await fetch(`/api/dashboard/slow-requests?${params.toString()}`).then(r=>r.json());
        if (!Array.isArray(data)) {
            document.getElementById('slow-body').innerHTML = `<tr><td colspan="8" class="text-danger">Ошибка формата данных</td></tr>`;
            return;
        }
        if (slowDT) slowDT.destroy();
        slowDT = $('#slowTable').DataTable({
            data: data.map(r => [
                formatDate(r.request_ts),
                formatDate(r.response_ts),
                r.method,
                r.action || '-',
                r.device,
                r.terminal || '-',
                r.processing_ms,
                r.request_id
            ]),
            order: [[6, 'desc']],
            language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
            pageLength: 50
        });
    } catch (e) {
        document.getElementById('slow-body').innerHTML = `<tr><td colspan="8" class="text-danger">Ошибка: ${e.message}</td></tr>`;
    }
}

// ===== Осиротевшие =====
async function loadOrphans() {
    document.getElementById('orphans-body').innerHTML = '<tr><td colspan="7" class="loading">Загрузка...</td></tr>';
    try {
        const params = buildFilterParams({ limit: 500 });
        const data = await fetch(`/api/dashboard/orphans?${params.toString()}`).then(r=>r.json());
        if (orphansDT) orphansDT.destroy();
        orphansDT = $('#orphansTable').DataTable({
            data: data.map(r => [
                r.request_id, r.method, r.device, r.terminal || '-',
                statusBadge(r.pair_status), formatDate(r.event_ts), r.session_id || '-'
            ]),
            order: [[5, 'desc']],
            language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
            pageLength: 50
        });
    } catch (e) {
        document.getElementById('orphans-body').innerHTML = `<tr><td colspan="7" class="text-danger">Ошибка: ${e.message}</td></tr>`;
    }
}

// ===== График (поминутно) =====
async function loadMinutely() {
    try {
        const params = new URLSearchParams({ minutes: '1440' });
        if (currentFilters.method) params.set('method', currentFilters.method);
        if (currentFilters.device) params.set('device', currentFilters.device);
        if (currentFilters.startDate) params.set('start_date', currentFilters.startDate);
        if (currentFilters.endDate) params.set('end_date', currentFilters.endDate);

        const data = await fetch(`/api/dashboard/minutely?${params.toString()}`).then(r=>r.json());
        const byMinute = {};
        data.forEach(r => {
            const dateObj = new Date(r.minute_ts);
            const timeLabel = dateObj.toLocaleString('ru-RU', {
                month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'
            });
            if (!byMinute[timeLabel]) byMinute[timeLabel] = { avg: [], p95: [], max: [], messages: 0 };
            if (r.avg_ms) byMinute[timeLabel].avg.push(r.avg_ms);
            if (r.p95_ms) byMinute[timeLabel].p95.push(r.p95_ms);
            if (r.max_ms) byMinute[timeLabel].max.push(r.max_ms);
            byMinute[timeLabel].messages += r.messages;
        });
        const labels = Object.keys(byMinute).sort((a, b) => a.localeCompare(b, undefined, {numeric: true}));
        const avgData = labels.map(h => byMinute[h].avg.reduce((a,b)=>a+b,0) / byMinute[h].avg.length);
        const p95Data = labels.map(h => Math.max(...byMinute[h].p95));
        const maxData = labels.map(h => Math.max(...byMinute[h].max));

        if (hourlyChart) hourlyChart.destroy();
        hourlyChart = new Chart(document.getElementById('hourlyChart'), {
            type: 'line',
            data: {
                labels: labels,
                datasets: [
                    { label: 'Среднее (мс)', data: avgData, borderColor: '#0d6efd', backgroundColor: 'rgba(13, 110, 253, 0.1)', fill: true, tension: 0.3 },
                    { label: 'P95 (мс)', data: p95Data, borderColor: '#ffc107', borderDash: [5, 5], fill: false, tension: 0.3 },
                    { label: 'Макс (мс)', data: maxData, borderColor: '#dc3545', fill: false, tension: 0.3 }
                ]
            },
            options: {
                responsive: true, maintainAspectRatio: false,
                scales: {
                    y: { beginAtZero: true, title: { display: true, text: 'Время (мс)' } },
                    x: { title: { display: true, text: 'Время' }, ticks: { maxRotation: 45, minRotation: 45, autoSkip: true, maxTicksLimit: 24 } }
                },
                plugins: {
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                let label = context.dataset.label || '';
                                if (label) label += ': ';
                                if (context.parsed.y !== null) label += context.parsed.y.toFixed(1) + ' мс';
                                const msgCount = byMinute[context.label]?.messages || 0;
                                return [label, `Всего запросов: ${msgCount}`];
                            }
                        }
                    }
                }
            }
        });
    } catch (e) { console.error('Ошибка графика:', e); }
}

// ===== Статистика методов =====
async function loadStats() {
    document.getElementById('stats-body').innerHTML = '<tr><td colspan="9" class="loading">Загрузка...</td></tr>';
    try {
        const params = buildCommonParams();
        const response = await fetch(`/api/stats/methods?${params.toString()}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        if (!Array.isArray(data)) {
            console.error('Ожидался массив, получено:', data);
            document.getElementById('stats-body').innerHTML = `<tr><td colspan="9" class="text-danger">Ошибка формата данных от API</td></tr>`;
            return;
        }

        if (statsDT) statsDT.destroy();
        statsDT = $('#statsTable').DataTable({
            data: data.map(r => [
                r.method, r.paired, r.no_response, r.no_request,
                r.avg_ms, r.p50_ms, r.p95_ms, r.p99_ms, r.max_ms
            ]),
            order: [[8, 'desc']],
            language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
            pageLength: 50
        });
    } catch (e) {
        console.error('Ошибка loadStats:', e);
        document.getElementById('stats-body').innerHTML = `<tr><td colspan="9" class="text-danger">Ошибка: ${e.message}</td></tr>`;
    }
}

// ===== Статистика ошибок по ТСД =====
async function loadErrorStats() {
    document.getElementById('error-stats-body').innerHTML = '<tr><td colspan="8" class="loading">Загрузка...</td></tr>';
    try {
        const params = buildCommonParams();
        const response = await fetch(`/api/stats/errors-by-device?${params.toString()}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        if (!Array.isArray(data)) {
            console.error('Ожидался массив, получено:', data);
            document.getElementById('error-stats-body').innerHTML = `<tr><td colspan="8" class="text-danger">Ошибка формата данных</td></tr>`;
            return;
        }

        if (errorStatsDT) errorStatsDT.destroy();
        errorStatsDT = $('#errorStatsTable').DataTable({
            data: data.map(r => [
                r.device || '-',
                r.method || '-',
                `<span class="badge bg-danger">${r.error_count}</span>`,
                r.unique_terminals || 0,
                r.unique_users || 0,
                formatDate(r.first_error),
                formatDate(r.last_error),
                `<div class="text-truncate" style="max-width: 300px;" title="${escapeAttr(r.top_error_sample || '')}">${escapeHtml((r.top_error_sample || '').substring(0, 100))}...</div>`
            ]),
            order: [[2, 'desc']],
            language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
            pageLength: 50,
            lengthMenu: [25, 50, 100, 250]
        });
    } catch (e) {
        console.error('Ошибка loadErrorStats:', e);
        document.getElementById('error-stats-body').innerHTML = `<tr><td colspan="8" class="text-danger">Ошибка: ${e.message}</td></tr>`;
    }
}

// ===== Топ по ТСД =====
async function loadTop() {
    document.getElementById('top-body').innerHTML = '<tr><td colspan="6" class="loading">Загрузка...</td></tr>';
    try {
        const params = buildCommonParams({ limit: 100 });
        const response = await fetch(`/api/dashboard/slow-methods?${params.toString()}`);

        if (!response.ok) {
            throw new Error(`HTTP ${response.status}: ${response.statusText}`);
        }

        const data = await response.json();

        if (!Array.isArray(data)) {
            console.error('Ожидался массив, получено:', data);
            document.getElementById('top-body').innerHTML = `<tr><td colspan="6" class="text-danger">Ошибка формата данных</td></tr>`;
            return;
        }

        if (topDT) topDT.destroy();
        topDT = $('#topTable').DataTable({
            data: data.map(r => [r.device, r.method, r.paired, r.avg_ms, r.max_ms, r.total_ms]),
            order: [[5, 'desc']],
            language: { url: "//cdn.datatables.net/plug-ins/1.13.6/i18n/ru.json" },
            pageLength: 50
        });
    } catch (e) {
        console.error('Ошибка loadTop:', e);
        document.getElementById('top-body').innerHTML = `<tr><td colspan="6" class="text-danger">Ошибка: ${e.message}</td></tr>`;
    }
}

// ===== Копирование в буфер =====
function copyToClipboard(text) {
    return new Promise((resolve) => {
        if (navigator.clipboard && window.isSecureContext) {
            navigator.clipboard.writeText(text)
                .then(() => resolve(true))
                .catch(() => resolve(false));
            return;
        }

        const textArea = document.createElement("textarea");
        textArea.value = text;
        textArea.style.position = "fixed";
        textArea.style.top = "0";
        textArea.style.left = "0";
        textArea.style.width = "2em";
        textArea.style.height = "2em";
        textArea.style.padding = "0";
        textArea.style.border = "none";
        textArea.style.outline = "none";
        textArea.style.boxShadow = "none";
        textArea.style.background = "transparent";
        textArea.style.opacity = "0";

        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();

        try {
            const successful = document.execCommand('copy');
            resolve(successful);
        } catch (err) {
            console.error('Fallback copy failed:', err);
            resolve(false);
        }

        document.body.removeChild(textArea);
    });
}

// ===== Health check =====
async function checkHealth() {
    try {
        const res = await fetch('/api/health', { signal: AbortSignal.timeout(5000) });

        if (!res.ok) throw new Error(`HTTP ${res.status}`);

        const data = await res.json();

        document.getElementById('stat-events').textContent =
            (data.events_total || 0).toLocaleString('ru-RU');

        const fmt = (ts) => ts ? ts.substring(0, 16).replace('T', ' ') : '—';
        document.getElementById('stat-last-import').textContent = fmt(data.last_import_at);
        document.getElementById('stat-max-event').textContent = fmt(data.max_event_ts);

        const statusLabel = document.querySelector('#db-status .text-white.fw-bold');
        const icon = statusLabel.querySelector('i');

        if (data.max_event_ts) {
            const maxDate = new Date(data.max_event_ts);
            const now = new Date();
            const hoursDiff = (now - maxDate) / (1000 * 60 * 60);

            icon.className = 'bi me-1';
            statusLabel.classList.remove('text-warning', 'text-danger');

            if (hoursDiff > 24) {
                icon.classList.add('bi-database-fill-x', 'text-danger');
                statusLabel.classList.add('text-danger');
            } else if (hoursDiff > 2) {
                icon.classList.add('bi-database-fill-exclamation', 'text-warning');
                statusLabel.classList.add('text-warning');
            } else {
                icon.classList.add('bi-database-fill-check', 'text-success');
            }
        }
    } catch (e) {
        console.warn('⚠️ Health check failed:', e.message);

        const statusDiv = document.getElementById('db-status');
        statusDiv.innerHTML = `
            <span class="text-warning fw-bold">
                <i class="bi bi-hourglass-split me-1"></i>
                Идёт импорт и обновление данных... <small class="text-white-50">(страница обновится автоматически)</small>
            </span>
        `;
    }
}

// ===== Обработчики переключения вкладок =====
document.querySelectorAll('[data-bs-toggle="tab"]').forEach(tabEl => {
    tabEl.addEventListener('shown.bs.tab', function (event) {
        const targetId = event.target.getAttribute('data-bs-target');

        if (targetId === '#tab-errors') {
            setTimeout(() => {
                if (!errorsDT) {
                    initErrorsTable();
                } else {
                    errorsDT.ajax.reload();
                }
                setTimeout(() => {
                    if (errorsDT) errorsDT.columns.adjust();
                }, 150);
            }, 100);
        }

        setTimeout(() => {
            if (targetId === '#tab-pairs' && pairsDT) pairsDT.columns.adjust();
            if (targetId === '#tab-slow' && slowDT) slowDT.columns.adjust();
            if (targetId === '#tab-orphans' && orphansDT) orphansDT.columns.adjust();
            if (targetId === '#tab-top' && topDT) topDT.columns.adjust();
            if (targetId === '#tab-stats' && statsDT) statsDT.columns.adjust();
            if (targetId === '#tab-error-stats' && errorStatsDT) errorStatsDT.columns.adjust();
        }, 100);
    });
});

// Автообновление статуса каждые 60 секунд
setInterval(checkHealth, 60000);

// ===== Инициализация при загрузке DOM =====
document.addEventListener('DOMContentLoaded', () => {
    console.log("🟢 DOM полностью загружен, начинаем инициализацию");
    checkHealth();
    loadFilters();
    loadAll();

    // Tooltips Bootstrap
    var tooltipTriggerList = [].slice.call(document.querySelectorAll('[data-bs-toggle="tooltip"]'));
    var tooltipList = tooltipTriggerList.map(function (tooltipTriggerEl) {
        return new bootstrap.Tooltip(tooltipTriggerEl);
    });

    // Flatpickr
    const def = getDefaultDateRange();
    dateRangePicker = flatpickr("#filter-date-range", {
        mode: "range",
        enableTime: true,
        enableSeconds: true,
        time_24hr: true,
        minuteIncrement: 1,
        dateFormat: "Y-m-d H:i:S",
        locale: "ru",
        defaultDate: [def.start, def.end],
        allowInput: true,
        onChange: function(selectedDates, dateStr, instance) {
            if (selectedDates.length === 2) {
                currentFilters.startDate = formatDateTimeLocal(selectedDates[0]);
                currentFilters.endDate = formatDateTimeLocal(selectedDates[1]);
            }
        }
    });

    document.getElementById('filter-date-range').addEventListener('change', function() {
        if (dateRangePicker.selectedDates.length === 2) {
            currentFilters.startDate = formatDateTimeLocal(dateRangePicker.selectedDates[0]);
            currentFilters.endDate = formatDateTimeLocal(dateRangePicker.selectedDates[1]);
        }
    });
});

// ===== Обработчики кликов по строкам таблиц =====

// Клик по строке таблицы пар
$(document).on('click', '#pairsTable tbody tr', function(e) {
    if ($(e.target).closest('.btn, a').length) return;
    const rowData = pairsDT.row(this).data();
    if (rowData && rowData[6]) {
        showDetails.call(this, rowData[6], rowData[4], rowData[0]);
    }
});

// Клик по строке таблицы медленных запросов
$(document).on('click', '#slowTable tbody tr', function(e) {
    if ($(e.target).closest('.btn, a').length) return;
    const rowData = slowDT.row(this).data();
    if (rowData && rowData[7]) {
        showDetails.call(null, rowData[7], rowData[4], rowData[0]);
    }
});

// Клик по строке таблицы осиротевших
$(document).on('click', '#orphansTable tbody tr', function(e) {
    if ($(e.target).closest('.btn, a').length) return;
    const rowData = orphansDT.row(this).data();
    if (rowData && rowData[0]) {
        showDetails.call(null, rowData[0], rowData[2], rowData[5]);
    }
});

// Клик по строке таблицы ошибок
$(document).on('click', '#errorsTable tbody tr', function(e) {
    if ($(e.target).closest('.btn, a').length) return;
    const rowData = errorsDT.row(this).data();
    if (rowData && rowData[5]) {
        const rawErrorText = rowData[5].filter;
        if (rawErrorText) {
            showErrorDetails(rawErrorText);
        }
    }
});

// ===== Обработчики копирования =====
$(document).ready(function() {
    console.log('✅ jQuery готов, вешаем обработчик на .copy-json-btn');

    $(document).on('click', '.copy-json-btn', async function(e) {
        e.preventDefault();
        e.stopPropagation();

        const targetId = $(this).attr('data-target');
        const container = document.getElementById(targetId);
        if (!container) return;

        const preElement = container.querySelector('pre');
        const textToCopy = preElement ? preElement.innerText : container.innerText;

        let copySuccess = false;

        if (navigator.clipboard && window.isSecureContext) {
            try {
                await navigator.clipboard.writeText(textToCopy);
                copySuccess = true;
            } catch (err) {
                console.warn('Clipboard API failed:', err);
            }
        }

        if (!copySuccess) {
            const range = document.createRange();
            range.selectNodeContents(preElement);
            const selection = window.getSelection();
            selection.removeAllRanges();
            selection.addRange(range);

            try {
                await new Promise(resolve => setTimeout(resolve, 50));
                copySuccess = document.execCommand('copy');
            } catch (err) {
                console.error('execCommand error:', err);
            }

            setTimeout(() => selection.removeAllRanges(), 1500);
        }

        const btn = $(this);
        if (copySuccess) {
            const originalHTML = btn.html();
            btn.html('✅ Скопировано!').removeClass('btn-outline-secondary').addClass('btn-success');
            setTimeout(() => {
                btn.html(originalHTML).removeClass('btn-success').addClass('btn-outline-secondary');
            }, 2000);
        } else {
            alert('⚠️ Браузер заблокировал фоновое копирование.\n\nТекст уже выделен! Нажмите Ctrl + C.');
        }
    });
});

// Копирование для вкладки "Примеры API"
$(document).on('click', '.copy-api-btn', async function(e) {
    e.preventDefault();
    const btn = $(this);
    const textToCopy = btn.attr('data-clipboard');

    if (!textToCopy) return;

    let copySuccess = false;

    if (navigator.clipboard && window.isSecureContext) {
        try {
            await navigator.clipboard.writeText(textToCopy);
            copySuccess = true;
        } catch (err) {
            console.warn('Clipboard API failed:', err);
        }
    }

    if (!copySuccess) {
        const textArea = document.createElement("textarea");
        textArea.value = textToCopy;
        textArea.style.position = "fixed";
        textArea.style.left = "-9999px";
        document.body.appendChild(textArea);
        textArea.focus();
        textArea.select();
        try {
            copySuccess = document.execCommand('copy');
        } catch (err) {
            console.error('execCommand error:', err);
        }
        document.body.removeChild(textArea);
    }

    if (copySuccess) {
        const originalHTML = btn.html();
        btn.html('✅ Скопировано!').removeClass('btn-outline-secondary').addClass('btn-success');
        setTimeout(() => {
            btn.html(originalHTML).removeClass('btn-success').addClass('btn-outline-secondary');
        }, 2000);
    } else {
        alert('Не удалось скопировать автоматически. Выделите текст вручную и нажмите Ctrl+C.');
    }
});