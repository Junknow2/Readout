// Readout - Running Optimized Audio PWA Client
(() => {
  'use strict';

  // -------------------------------------------------------------
  // State
  // -------------------------------------------------------------
  let queue = [];
  let queueFilter = 'unread'; // 'unread' | 'listened'
  let currentIndex = -1;
  let currentTrackId = null;
  let isPlaying = false;
  let cachedAudioUrls = new Set();
  const speeds = [1.0, 1.25, 1.5, 2.0];
  let currentSpeedIdx = 0;

  // Tab State
  let activeTab = 'queue';

  // Curator State
  let curatorDates = [];
  let curatorSelectedDate = '';
  let curatorDaily = null;
  let curatorResearchLog = null;
  let curatorStatus = null;
  let curatorSubTab = 'daily';
  let curatorLoadedOnce = false;

  // -------------------------------------------------------------
  // DOM Elements
  // -------------------------------------------------------------
  const audio = document.getElementById('audio-player');
  const networkBadge = document.getElementById('network-badge');
  const toast = document.getElementById('toast');
  const btnRss = document.getElementById('btn-rss');
  const btnBookmarklet = document.getElementById('btn-bookmarklet');
  const btnBookmarkletTip = document.getElementById('btn-bookmarklet-tip');
  const modalBookmarklet = document.getElementById('modal-bookmarklet');
  const modalClose = document.getElementById('modal-close');
  const bookmarkletLink = document.getElementById('bookmarklet-link');
  const bookmarkletCodeInput = document.getElementById('bookmarklet-code-input');
  const btnCopyBookmarklet = document.getElementById('btn-copy-bookmarklet');

  // Tabs DOM
  const tabBtnQueue = document.getElementById('tab-btn-queue');
  const tabBtnCurator = document.getElementById('tab-btn-curator');
  const tabBtnAdd = document.getElementById('tab-btn-add');
  const paneQueue = document.getElementById('pane-queue');
  const paneCurator = document.getElementById('pane-curator');
  const paneAdd = document.getElementById('pane-add');

  // Curator DOM
  const curatorDateSelect = document.getElementById('curator-date-select');
  const btnCuratorRun = document.getElementById('btn-curator-run');
  const curatorStatusBanner = document.getElementById('curator-status-banner');
  const curatorStatusMsg = document.getElementById('curator-status-msg');
  const curatorStatusPct = document.getElementById('curator-status-pct');
  const curatorProgressBar = document.getElementById('curator-progress-bar');
  const subtabDaily = document.getElementById('subtab-daily');
  const subtabLog = document.getElementById('subtab-log');
  const curatorDailyView = document.getElementById('curator-daily-view');
  const curatorLogView = document.getElementById('curator-log-view');
  const curatorDailyList = document.getElementById('curator-daily-list');
  const curatorLogSummary = document.getElementById('curator-log-summary');
  const curatorLogList = document.getElementById('curator-log-list');

  // Queue View DOM
  const urlInput = document.getElementById('url-input');
  const btnAddUrl = document.getElementById('btn-add-url');
  const queueList = document.getElementById('queue-list');
  const queueCounter = document.getElementById('queue-counter');
  const queueFilterUnread = document.getElementById('queue-filter-unread');
  const queueFilterListened = document.getElementById('queue-filter-listened');
  const pdfDropzone = document.getElementById('pdf-dropzone');
  const pdfFileInput = document.getElementById('pdf-file-input');
  const btnPdfBrowse = document.getElementById('btn-pdf-browse');
  const pdfUploadTrigger = document.getElementById('pdf-upload-trigger');

  // Player DOM
  const playerTitle = document.getElementById('player-title');
  const playerSummary = document.getElementById('player-summary');
  const trackStatusTag = document.getElementById('track-status-tag');
  const btnSpeed = document.getElementById('btn-speed');
  const progressWrap = document.getElementById('progress-wrap');
  const progressFill = document.getElementById('progress-fill');
  const timeCurrent = document.getElementById('time-current');
  const timeDuration = document.getElementById('time-duration');
  const btnPlayPause = document.getElementById('btn-play-pause');
  const iconPlay = document.getElementById('icon-play');
  const iconPause = document.getElementById('icon-pause');
  const btnSeekBack = document.getElementById('btn-seek-back');
  const btnSeekForward = document.getElementById('btn-seek-forward');
  const btnPrev = document.getElementById('btn-prev');
  const btnNext = document.getElementById('btn-next');

  // -------------------------------------------------------------
  // PWA Service Worker Registration & Offline Detection
  // -------------------------------------------------------------
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js')
      .then((reg) => {
        console.log('[App] Service Worker registered with scope:', reg.scope);
      })
      .catch((err) => {
        console.warn('[App] SW registration failed:', err);
      });

    navigator.serviceWorker.addEventListener('message', (event) => {
      if (event.data && event.data.type === 'AUDIO_CACHED') {
        cachedAudioUrls.add(event.data.url);
        renderQueue();
      }
    });
  }

  function updateNetworkStatus() {
    if (navigator.onLine) {
      networkBadge.textContent = 'ONLINE';
      networkBadge.className = 'status-badge online';
    } else {
      networkBadge.textContent = 'OFFLINE';
      networkBadge.className = 'status-badge offline';
      showToast('オフラインモード（キャッシュ済み音声のみ再生可能）');
    }
  }

  window.addEventListener('online', updateNetworkStatus);
  window.addEventListener('offline', updateNetworkStatus);
  updateNetworkStatus();

  // -------------------------------------------------------------
  // Helpers
  // -------------------------------------------------------------
  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => {
      toast.classList.remove('show');
    }, 2800);
  }

  function formatTime(seconds) {
    if (isNaN(seconds) || seconds < 0) return '0:00';
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
  }

  function escapeHtml(str) {
    if (!str) return '';
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function renderEngineBadge(engine) {
    if (engine === 'voicepeak') {
      return '<span class="badge-tts-engine voicepeak" title="VOICEPEAKで生成">🎙️ VOICEPEAK</span>';
    }
    if (engine === 'edge_tts') {
      return '<span class="badge-tts-engine edge-tts" title="Edge-TTSで生成">🔊 Edge-TTS</span>';
    }
    return '';
  }

  // -------------------------------------------------------------
  // Tab Switcher (3 tabs: queue | curator | add)
  // -------------------------------------------------------------
  function switchTab(target) {
    if (target === activeTab) return;
    activeTab = target;

    const tabs = [
      { key: 'queue', btn: tabBtnQueue, pane: paneQueue },
      { key: 'curator', btn: tabBtnCurator, pane: paneCurator },
      { key: 'add', btn: tabBtnAdd, pane: paneAdd },
    ];
    tabs.forEach(({ key, btn, pane }) => {
      if (!btn || !pane) return;
      const isActive = key === target;
      btn.classList.toggle('active', isActive);
      btn.setAttribute('aria-selected', isActive ? 'true' : 'false');
      pane.style.display = isActive ? 'flex' : 'none';
    });

    if (target === 'curator') {
      if (!curatorLoadedOnce) {
        curatorLoadedOnce = true;
        fetchCuratorDaily();
      } else {
        fetchCuratorStatus();
      }
    }
  }

  tabBtnQueue.addEventListener('click', () => switchTab('queue'));
  if (tabBtnCurator) tabBtnCurator.addEventListener('click', () => switchTab('curator'));
  if (tabBtnAdd) tabBtnAdd.addEventListener('click', () => switchTab('add'));

  // -------------------------------------------------------------
  // Cache Verification & Auto Pre-caching
  // -------------------------------------------------------------
  async function checkOfflineCache() {
    if (!('caches' in window)) return;
    try {
      const cache = await caches.open('readout-v9');
      for (const item of queue) {
        if (item.has_audio !== false && !item.audio_deleted && item.status === 'ready' && item.audio_filename) {
          const audioUrl = `/audio/${item.audio_filename}`;
          const match = await cache.match(audioUrl);
          if (match) {
            cachedAudioUrls.add(audioUrl);
          } else if (navigator.onLine && navigator.serviceWorker && navigator.serviceWorker.controller) {
            navigator.serviceWorker.controller.postMessage({
              type: 'CACHE_AUDIO',
              url: audioUrl
            });
          }
        }
      }
    } catch (e) {
      console.warn('Cache check error:', e);
    }
  }

  // -------------------------------------------------------------
  // API Calls: Queue
  // -------------------------------------------------------------
  function switchQueueFilter(filter) {
    if (filter !== 'unread' && filter !== 'listened') return;
    if (queueFilter === filter) return;
    queueFilter = filter;
    if (queueFilterUnread) {
      queueFilterUnread.classList.toggle('active', filter === 'unread');
      queueFilterUnread.setAttribute('aria-selected', filter === 'unread' ? 'true' : 'false');
    }
    if (queueFilterListened) {
      queueFilterListened.classList.toggle('active', filter === 'listened');
      queueFilterListened.setAttribute('aria-selected', filter === 'listened' ? 'true' : 'false');
    }
    fetchQueue();
  }

  if (queueFilterUnread) {
    queueFilterUnread.addEventListener('click', () => switchQueueFilter('unread'));
  }
  if (queueFilterListened) {
    queueFilterListened.addEventListener('click', () => switchQueueFilter('listened'));
  }

  async function fetchQueue() {
    try {
      const endpoint = queueFilter === 'listened' ? '/api/queue?filter=listened' : '/api/queue';
      const res = await fetch(endpoint);
      if (!res.ok) throw new Error('Failed to fetch queue');
      const data = await res.json();
      
      queue = data;
      if (currentTrackId) {
        currentIndex = queue.findIndex(it => it.id === currentTrackId);
      } else {
        currentIndex = -1;
      }

      renderQueue();
      if (queueFilter === 'unread') {
        checkOfflineCache();
      }

      // If nothing is loaded yet and we are on unread queue, set up the first ready item
      if (!currentTrackId && queueFilter === 'unread' && queue.length > 0) {
        const firstReady = queue.findIndex(it => it.status === 'ready');
        if (firstReady !== -1) {
          loadTrack(firstReady, false);
        }
      }
    } catch (err) {
      console.warn('Queue fetch error (may be offline):', err);
    }
  }

  async function addUrl(url) {
    try {
      showToast('記事をキューに追加中...');
      const res = await fetch('/api/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'URL追加に失敗しました');
      }
      urlInput.value = '';
      showToast('キューに追加しました。音声を生成しています...');
      if (queueFilter !== 'unread') {
        switchQueueFilter('unread');
      }
      switchTab('queue');
      await fetchQueue();
    } catch (err) {
      alert(`エラー: ${err.message}`);
    }
  }

  async function handlePdfUpload(file) {
    if (!file) return;
    const isPdf = file.name.toLowerCase().endsWith('.pdf') || (file.type && file.type.includes('pdf'));
    if (!isPdf) {
      showToast('⚠️ PDFファイル（.pdf）を指定してください');
      return;
    }

    try {
      showToast(`📁 「${file.name}」をアップロード中...`);
      const formData = new FormData();
      formData.append('file', file);

      const res = await fetch('/api/upload-pdf', {
        method: 'POST',
        body: formData
      });

      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'アップロードに失敗しました' }));
        throw new Error(err.detail || 'アップロードに失敗しました');
      }

      showToast(`✅ 「${file.name}」をキューに追加しました。Gemini解析中...`);
      if (pdfFileInput) pdfFileInput.value = '';
      if (queueFilter !== 'unread') {
        switchQueueFilter('unread');
      }
      switchTab('queue');
      await fetchQueue();
    } catch (err) {
      alert(`PDFアップロードエラー: ${err.message}`);
    }
  }

  async function markListened(itemId) {
    try {
      await fetch(`/api/queue/${itemId}/listened`, { method: 'POST' });
      showToast('既読にしました（「既読」タブから振り返り可能）');
      if (currentTrackId === itemId) {
        audio.pause();
        isPlaying = false;
        updatePlayPauseUI();
        const nextIdx = queue.findIndex((it, idx) => idx > currentIndex && it.status === 'ready');
        if (nextIdx !== -1) {
          loadTrack(nextIdx, true);
        } else {
          resetPlayer();
        }
      }
      await fetchQueue();
    } catch (err) {
      console.error(err);
    }
  }

  async function markUnread(itemId) {
    try {
      const res = await fetch(`/api/queue/${itemId}/unread`, { method: 'POST' });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || '未読へ戻せませんでした');
      }
      showToast('未読キューへ戻しました');
      await fetchQueue();
    } catch (err) {
      showToast(`エラー: ${err.message}`);
    }
  }

  async function sendQueueItemToMatrixNote(item, btn) {
    if (item.sent_to_matrix_note) {
      showToast('すでに Matrix Note へ送信済みです');
      return;
    }
    const origHtml = btn.innerHTML;
    btn.disabled = true;
    btn.textContent = '⏳ 送信中...';
    try {
      const res = await fetch(`/api/queue/${item.id}/send-to-matrix-note`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force: false }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || 'Matrix Note 送信に失敗しました');
      }
      const data = await res.json();
      item.sent_to_matrix_note = true;
      item.matrix_note_id = data.note_id || item.matrix_note_id;
      btn.classList.add('sent');
      btn.innerHTML = '✅ Matrix済';
      showToast(data.already_sent ? 'すでに Matrix Note へ送信済みです' : '📝 Matrix Note へメモを送信しました！');
    } catch (err) {
      showToast(`エラー: ${err.message}`);
      btn.disabled = false;
      btn.innerHTML = origHtml;
    }
  }

  async function deleteItem(itemId) {
    if (!confirm('このアイテムを削除しますか？')) return;
    try {
      await fetch(`/api/queue/${itemId}`, { method: 'DELETE' });
      showToast('アイテムを削除しました');
      if (currentTrackId === itemId) {
        audio.pause();
        isPlaying = false;
        updatePlayPauseUI();
        resetPlayer();
      }
      await fetchQueue();
    } catch (err) {
      console.error(err);
    }
  }

  // -------------------------------------------------------------
  // Audio Player Management
  // -------------------------------------------------------------
  function isItemPlayable(item) {
    if (!item) return false;
    if (item.audio_deleted || item.has_audio === false) return false;
    return item.status === 'ready' || item.status === 'listened';
  }

  function loadTrack(index, autoPlay = true) {
    if (index < 0 || index >= queue.length) return;
    const item = queue[index];
    if (!isItemPlayable(item)) {
      if (item.audio_deleted || item.has_audio === false) {
        showToast('音声ファイルは削除済みです（テキスト保持）');
      } else {
        showToast('このアイテムはまだ生成中です');
      }
      return;
    }

    currentIndex = index;
    currentTrackId = item.id;
    const audioUrl = `/audio/${item.audio_filename}`;
    audio.src = audioUrl;
    audio.playbackRate = speeds[currentSpeedIdx];

    playerTitle.textContent = item.title;
    playerSummary.textContent = item.summary || 'サマリーなし';
    trackStatusTag.textContent = `TRACK ${currentIndex + 1}/${queue.length}`;
    timeDuration.textContent = formatTime(item.audio_duration_sec || 0);

    updateMediaSession(item);
    renderQueue();

    if (autoPlay) {
      audio.play().then(() => {
        isPlaying = true;
        updatePlayPauseUI();
      }).catch(err => {
        console.warn('AutoPlay blocked:', err);
        isPlaying = false;
        updatePlayPauseUI();
      });
    } else {
      isPlaying = false;
      updatePlayPauseUI();
    }
  }

  function resetPlayer() {
    currentIndex = -1;
    currentTrackId = null;
    audio.src = '';
    playerTitle.textContent = '再生待ちのアイテムがありません';
    playerSummary.textContent = '「追加」タブや Curator から記事・PDFを追加すると、AI音声化してここで再生できます。';
    trackStatusTag.textContent = 'IDLE';
    timeCurrent.textContent = '0:00';
    timeDuration.textContent = '0:00';
    progressFill.style.width = '0%';
    renderQueue();
  }

  function updatePlayPauseUI() {
    if (isPlaying) {
      iconPlay.style.display = 'none';
      iconPause.style.display = 'block';
    } else {
      iconPlay.style.display = 'block';
      iconPause.style.display = 'none';
    }
  }

  function togglePlayPause() {
    if (currentIndex === -1) {
      const firstReady = queue.findIndex(it => it.status === 'ready');
      if (firstReady !== -1) {
        loadTrack(firstReady, true);
      } else {
        showToast('再生できるアイテムがありません');
      }
      return;
    }

    if (audio.paused) {
      audio.play().then(() => {
        isPlaying = true;
        updatePlayPauseUI();
      }).catch(err => {
        console.warn('Playback error:', err);
      });
    } else {
      audio.pause();
      isPlaying = false;
      updatePlayPauseUI();
    }
  }

  function seekRelative(seconds) {
    if (!audio.src) return;
    const newTime = Math.max(0, Math.min(audio.currentTime + seconds, audio.duration || 0));
    audio.currentTime = newTime;
    showToast(seconds > 0 ? `+${seconds}s 進む` : `${seconds}s 戻る`);
  }

  function playNext() {
    if (queue.length === 0) return;
    let nextIdx = -1;
    for (let i = currentIndex + 1; i < queue.length; i++) {
      if (queue[i].status === 'ready') {
        nextIdx = i;
        break;
      }
    }
    if (nextIdx !== -1) {
      loadTrack(nextIdx, true);
    } else {
      showToast('次の再生可能トラックがありません');
    }
  }

  function playPrev() {
    if (queue.length === 0) return;
    let prevIdx = -1;
    for (let i = currentIndex - 1; i >= 0; i--) {
      if (queue[i].status === 'ready') {
        prevIdx = i;
        break;
      }
    }
    if (prevIdx !== -1) {
      loadTrack(prevIdx, true);
    } else {
      showToast('前のトラックがありません');
    }
  }

  function cycleSpeed() {
    currentSpeedIdx = (currentSpeedIdx + 1) % speeds.length;
    const speed = speeds[currentSpeedIdx];
    audio.playbackRate = speed;
    btnSpeed.textContent = `${speed.toFixed(speed % 1 === 0 ? 1 : 2)}x`;
    showToast(`再生速度: ${btnSpeed.textContent}`);
  }

  // Progress Bar
  audio.addEventListener('timeupdate', () => {
    if (!audio.duration) return;
    const percent = (audio.currentTime / audio.duration) * 100;
    progressFill.style.width = `${percent}%`;
    timeCurrent.textContent = formatTime(audio.currentTime);
  });

  audio.addEventListener('ended', () => {
    isPlaying = false;
    updatePlayPauseUI();
    // Auto-advance to next ready track
    playNext();
  });

  // Touch & Click Seeking on Giant Progress Bar
  progressWrap.addEventListener('click', (e) => {
    if (!audio.duration) return;
    const rect = progressWrap.getBoundingClientRect();
    const ratio = Math.max(0, Math.min((e.clientX - rect.left) / rect.width, 1));
    audio.currentTime = ratio * audio.duration;
  });

  // -------------------------------------------------------------
  // MediaSession API (Lock Screen & Bluetooth Headset Integration)
  // -------------------------------------------------------------
  function updateMediaSession(item) {
    if (!('mediaSession' in navigator)) return;

    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title,
      artist: 'Readout - Runner Audio',
      album: 'Readout Daily Queue',
      artwork: [
        { src: '/static/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
        { src: '/static/icons/icon-512.png', sizes: '512x512', type: 'image/png' },
      ],
    });

    navigator.mediaSession.setActionHandler('play', () => {
      audio.play();
      isPlaying = true;
      updatePlayPauseUI();
    });
    navigator.mediaSession.setActionHandler('pause', () => {
      audio.pause();
      isPlaying = false;
      updatePlayPauseUI();
    });
    navigator.mediaSession.setActionHandler('seekbackward', () => seekRelative(-10));
    navigator.mediaSession.setActionHandler('seekforward', () => seekRelative(30));
    navigator.mediaSession.setActionHandler('previoustrack', playPrev);
    navigator.mediaSession.setActionHandler('nexttrack', playNext);
  }

  // -------------------------------------------------------------
  // Queue Rendering
  // -------------------------------------------------------------
  function renderQueue() {
    queueCounter.textContent = `${queue.length} 件`;

    if (queue.length === 0) {
      if (queueFilter === 'listened') {
        queueList.innerHTML = `
          <div class="queue-empty">
            <p class="queue-empty-title">既読アイテムはまだありません</p>
            <p>聴き終えた記事を「既読」にするとここに一覧表示されます（Matrix Note への送信や振り返りが可能です）。</p>
          </div>
        `;
      } else {
        queueList.innerHTML = `
          <div class="queue-empty">
            <p class="queue-empty-title">未読キューは空です</p>
            <p>「📥 追加」タブからURLやPDFを登録するか、Curator から追加してください。</p>
          </div>
        `;
      }
      return;
    }

    queueList.innerHTML = '';
    queue.forEach((item, index) => {
      const card = document.createElement('div');
      const isCurrent = item.id === currentTrackId;
      const isProcessing = item.status === 'processing' || item.status === 'pending';
      const isListened = item.status === 'listened';
      const isAudioDeleted = !!(item.audio_deleted || item.has_audio === false);
      const canPlay = isItemPlayable(item);
      const isCached = !isAudioDeleted && item.audio_filename && cachedAudioUrls.has(`/audio/${item.audio_filename}`);

      let cardClass = 'queue-card';
      if (isCurrent) cardClass += ' active-item';
      if (isProcessing) cardClass += ' processing-item';
      if (isListened) cardClass += ' listened-item';
      if (isAudioDeleted) cardClass += ' audio-deleted-item';
      card.className = cardClass;

      // Status Badge
      let statusBadgeHtml = '';
      if (item.status === 'pending') {
        statusBadgeHtml = `<span class="badge-status processing">待機中</span>`;
      } else if (item.status === 'processing') {
        statusBadgeHtml = `<span class="badge-status processing">生成中...</span>`;
      } else if (item.status === 'failed') {
        statusBadgeHtml = `<span class="badge-status failed">生成失敗</span>`;
      } else if (isListened && isAudioDeleted) {
        statusBadgeHtml = `<span class="badge-audio-deleted">音声削除済み（テキスト保持）</span>`;
      }

      // Multi-page Badge
      let pageBadgeHtml = '';
      if (item.page_count && item.page_count > 1) {
        pageBadgeHtml = `<span class="badge-status" style="background:rgba(59,130,246,0.18);color:#60a5fa;border:1px solid rgba(59,130,246,0.3);">全${item.page_count}ページ結合</span>`;
      }

      // PDF Badge
      let pdfBadgeHtml = '';
      const isPdfItem = item.item_type === 'pdf' || (item.url && item.url.startsWith('upload:')) || (item.url && item.url.toLowerCase().endsWith('.pdf'));
      if (isPdfItem) {
        pdfBadgeHtml = `<span class="badge-status" style="background:rgba(236,72,153,0.18);color:#f472b6;border:1px solid rgba(236,72,153,0.3);">📄 PDF</span>`;
      }

      // Offline Badge
      let offlineBadgeHtml = '';
      if (isCached) {
        offlineBadgeHtml = `<span class="badge-offline">⚡ オフライン済</span>`;
      }

      // TTS Engine Badge
      const engineBadgeHtml = renderEngineBadge(item.tts_engine);

      // Duration Badge
      let durationHtml = '';
      if (!isAudioDeleted && item.audio_duration_sec > 0) {
        durationHtml = `<span class="badge-duration">${formatTime(item.audio_duration_sec)}</span>`;
      }

      // Summary & Source Link
      const summaryHtml = item.summary
        ? `<p class="card-summary">${escapeHtml(item.summary)}</p>`
        : '';
      const hasHttpUrl = item.url && (item.url.startsWith('http://') || item.url.startsWith('https://'));
      const sourceRowHtml = hasHttpUrl
        ? `<div class="card-source-row">
             <a href="${escapeHtml(item.url)}" target="_blank" rel="noopener" class="card-source-link" title="${escapeHtml(item.url)}">🔗 ${escapeHtml(item.source_name || item.url)}</a>
           </div>`
        : '';

      // Matrix Note Button
      const isSentMatrix = !!item.sent_to_matrix_note;
      const matrixBtnHtml = isSentMatrix
        ? `<button class="btn-card-action btn-matrix-note sent" data-action="matrix" title="Matrix Note へ送信済み">✅ Matrix済</button>`
        : `<button class="btn-card-action btn-matrix-note" data-action="matrix" title="Matrix Note へメモ送信">📝 Matrix Note</button>`;

      // Status toggle button (Listened / Unread)
      let statusToggleBtnHtml = '';
      if (isListened) {
        if (!isAudioDeleted) {
          statusToggleBtnHtml = `
            <button class="btn-card-action btn-unread" data-action="unread" title="未読キューへ戻す">
              ↩ 未読に戻す
            </button>
          `;
        }
      } else {
        statusToggleBtnHtml = `
          <button class="btn-card-action btn-listened" data-action="listened" title="聴き終わった（既読一覧へ移動）">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
            既読
          </button>
        `;
      }

      card.innerHTML = `
        <div class="card-top">
          <h3 class="card-title">${escapeHtml(item.title)}</h3>
          <div class="card-meta">
            ${statusBadgeHtml}
            ${pdfBadgeHtml}
            ${pageBadgeHtml}
            ${engineBadgeHtml}
            ${offlineBadgeHtml}
            ${durationHtml}
          </div>
        </div>
        ${summaryHtml}
        ${sourceRowHtml}
        <div class="card-actions">
          ${matrixBtnHtml}
          ${statusToggleBtnHtml}
          <button class="btn-card-action btn-delete" data-action="delete" title="削除">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            削除
          </button>
        </div>
      `;

      // Click card to play (if playable)
      card.addEventListener('click', (e) => {
        if (e.target.closest('.card-actions') || e.target.closest('a')) return;
        if (canPlay) {
          loadTrack(index, true);
        } else if (isAudioDeleted) {
          showToast('音声ファイルは削除済みです（テキスト・出典情報は保持されています）');
        } else if (isProcessing) {
          showToast('現在GeminiとTTSで音声を生成中です。少々お待ちください。');
        } else if (item.status === 'failed') {
          showToast(`生成エラー: ${item.error_message || '不明なエラー'}`);
        }
      });

      // Actions
      const btnMatrix = card.querySelector('[data-action="matrix"]');
      if (btnMatrix) {
        btnMatrix.addEventListener('click', (e) => {
          e.stopPropagation();
          sendQueueItemToMatrixNote(item, btnMatrix);
        });
      }

      const btnListened = card.querySelector('[data-action="listened"]');
      if (btnListened) {
        btnListened.addEventListener('click', (e) => {
          e.stopPropagation();
          markListened(item.id);
        });
      }

      const btnUnread = card.querySelector('[data-action="unread"]');
      if (btnUnread) {
        btnUnread.addEventListener('click', (e) => {
          e.stopPropagation();
          markUnread(item.id);
        });
      }

      const btnDelete = card.querySelector('[data-action="delete"]');
      btnDelete.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteItem(item.id);
      });

      queueList.appendChild(card);
    });
  }

  // -------------------------------------------------------------
  // Controls Event Listeners
  // -------------------------------------------------------------
  btnPlayPause.addEventListener('click', togglePlayPause);
  btnSeekBack.addEventListener('click', () => seekRelative(-10));
  btnSeekForward.addEventListener('click', () => seekRelative(30));
  btnPrev.addEventListener('click', playPrev);
  btnNext.addEventListener('click', playNext);
  btnSpeed.addEventListener('click', cycleSpeed);

  btnAddUrl.addEventListener('click', () => {
    const url = urlInput.value.trim();
    if (url) {
      addUrl(url);
    } else {
      showToast('URLを入力してください');
    }
  });

  urlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const url = urlInput.value.trim();
      if (url) addUrl(url);
    }
  });

  btnRss.addEventListener('click', () => {
    const rssUrl = `${window.location.origin}/feed.xml`;
    if (navigator.clipboard) {
      navigator.clipboard.writeText(rssUrl).then(() => {
        showToast('RSS URLをコピーしました！Pocket Castsで登録できます');
      }).catch(() => {
        prompt('Pocket Casts 購読用 URL:', rssUrl);
      });
    } else {
      prompt('Pocket Casts 購読用 URL:', rssUrl);
    }
  });

  // -------------------------------------------------------------
  // Bookmarklet Modal & Code Generator
  // -------------------------------------------------------------
  function getBookmarkletCode() {
    const origin = (window.location.origin && window.location.origin !== 'null' && !window.location.origin.includes('file:')) 
      ? window.location.origin 
      : 'https://readout.nocial.jp';
    return `javascript:(function(){const u=encodeURIComponent(location.href);const t=encodeURIComponent(document.title);const w=window.open('${origin}/api/bookmarklet?url='+u+'&title='+t,'readout_popup','width=400,height=260,menubar=no,toolbar=no,location=no,status=no');if(w){w.focus();}})();`;
  }

  function setupBookmarklet() {
    const code = getBookmarkletCode();
    if (bookmarkletLink) {
      bookmarkletLink.setAttribute('href', code);
    }
    if (bookmarkletCodeInput) {
      bookmarkletCodeInput.value = code;
    }
  }

  function openBookmarkletModal() {
    setupBookmarklet();
    if (modalBookmarklet) {
      modalBookmarklet.style.display = 'flex';
    }
  }

  function closeBookmarkletModal() {
    if (modalBookmarklet) {
      modalBookmarklet.style.display = 'none';
    }
  }

  if (btnBookmarklet) {
    btnBookmarklet.addEventListener('click', openBookmarkletModal);
  }
  if (btnBookmarkletTip) {
    btnBookmarkletTip.addEventListener('click', openBookmarkletModal);
  }
  if (modalClose) {
    modalClose.addEventListener('click', closeBookmarkletModal);
  }
  if (modalBookmarklet) {
    modalBookmarklet.addEventListener('click', (e) => {
      if (e.target === modalBookmarklet) {
        closeBookmarkletModal();
      }
    });
  }
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && modalBookmarklet && modalBookmarklet.style.display !== 'none') {
      closeBookmarkletModal();
    }
  });

  if (bookmarkletLink) {
    bookmarkletLink.addEventListener('click', (e) => {
      e.preventDefault();
      showToast('このボタンをブラウザのブックマークバーにドラッグ＆ドロップしてください！');
    });
  }

  if (btnCopyBookmarklet && bookmarkletCodeInput) {
    btnCopyBookmarklet.addEventListener('click', () => {
      const code = bookmarkletCodeInput.value || getBookmarkletCode();
      if (navigator.clipboard) {
        navigator.clipboard.writeText(code).then(() => {
          showToast('ブックマークレットコードをコピーしました！');
        }).catch(() => {
          bookmarkletCodeInput.select();
          document.execCommand('copy');
          showToast('コードをコピーしました');
        });
      } else {
        bookmarkletCodeInput.select();
        document.execCommand('copy');
        showToast('コードをコピーしました');
      }
    });
  }

  // -------------------------------------------------------------
  // PDF Dropzone & Upload Setup
  // -------------------------------------------------------------
  function setupPdfUpload() {
    if (!pdfDropzone || !pdfFileInput) return;

    if (pdfUploadTrigger) {
      pdfUploadTrigger.addEventListener('click', (e) => {
        if (e.target !== btnPdfBrowse) {
          pdfFileInput.click();
        }
      });
    }

    if (btnPdfBrowse) {
      btnPdfBrowse.addEventListener('click', (e) => {
        e.stopPropagation();
        pdfFileInput.click();
      });
    }

    pdfFileInput.addEventListener('change', (e) => {
      if (e.target.files && e.target.files[0]) {
        handlePdfUpload(e.target.files[0]);
      }
    });

    // Drag & Drop
    ['dragenter', 'dragover'].forEach(eventName => {
      pdfDropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        pdfDropzone.classList.add('dragover');
      }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
      pdfDropzone.addEventListener(eventName, (e) => {
        e.preventDefault();
        e.stopPropagation();
        pdfDropzone.classList.remove('dragover');
      }, false);
    });

    pdfDropzone.addEventListener('drop', (e) => {
      const dt = e.dataTransfer;
      if (dt && dt.files && dt.files[0]) {
        handlePdfUpload(dt.files[0]);
      }
    });
  }

  // -------------------------------------------------------------
  // Personal Knowledge Curator UI & API
  // -------------------------------------------------------------
  const CURATOR_RATINGS = [
    { val: 5, label: '非常に面白かった' },
    { val: 4, label: '面白かった' },
    { val: 3, label: '普通' },
    { val: 2, label: 'あまり面白くなかった' },
    { val: 1, label: '既に知っていた' },
  ];
  const CURATOR_REASON_CHIPS = [
    '視点が新鮮',
    '根拠が強い',
    '実務に効く',
    '既知だった',
    '抽象的すぎ',
  ];
  const AXIS_LABELS = {
    question_depth: '問いの深さ',
    explanatory_power: '説明力',
    evidence_quality: '根拠の質',
    originality: '独自性',
    long_term_value: '長期的価値',
    curiosity: '好奇心',
    audio_suitability: '音声適性',
  };

  function switchCuratorSubTab(sub) {
    curatorSubTab = sub;
    if (subtabDaily) subtabDaily.classList.toggle('active', sub === 'daily');
    if (subtabLog) subtabLog.classList.toggle('active', sub === 'log');
    if (curatorDailyView) curatorDailyView.style.display = sub === 'daily' ? 'block' : 'none';
    if (curatorLogView) curatorLogView.style.display = sub === 'log' ? 'block' : 'none';
  }

  if (subtabDaily) subtabDaily.addEventListener('click', () => switchCuratorSubTab('daily'));
  if (subtabLog) subtabLog.addEventListener('click', () => switchCuratorSubTab('log'));

  if (curatorDateSelect) {
    curatorDateSelect.addEventListener('change', () => {
      curatorSelectedDate = curatorDateSelect.value;
      fetchCuratorDaily(curatorSelectedDate);
    });
  }

  if (btnCuratorRun) {
    btnCuratorRun.addEventListener('click', async () => {
      try {
        btnCuratorRun.disabled = true;
        const res = await fetch('/api/curator/run', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ synthesize_audio: true }),
        });
        const data = await res.json();
        showToast(data.message || 'キュレーションを開始しました');
        await fetchCuratorStatus();
      } catch (err) {
        showToast(`実行エラー: ${err.message}`);
        btnCuratorRun.disabled = false;
      }
    });
  }

  function renderCuratorStatusBanner(status) {
    if (!curatorStatusBanner || !status) return;
    if (status.running) {
      curatorStatusBanner.style.display = 'flex';
      if (curatorStatusMsg) curatorStatusMsg.textContent = status.message || 'キュレーション実行中...';
      if (curatorStatusPct) curatorStatusPct.textContent = `${status.progress || 0}%`;
      if (curatorProgressBar) curatorProgressBar.style.width = `${status.progress || 0}%`;
      if (btnCuratorRun) btnCuratorRun.disabled = true;
    } else {
      curatorStatusBanner.style.display = 'none';
      if (btnCuratorRun) btnCuratorRun.disabled = false;
    }
  }

  async function fetchCuratorStatus() {
    try {
      const res = await fetch('/api/curator/status');
      if (!res.ok) return;
      const prevRunning = curatorStatus && curatorStatus.running;
      const prevReady = curatorStatus ? curatorStatus.parts_ready : 0;
      curatorStatus = await res.json();
      renderCuratorStatusBanner(curatorStatus);
      // Refresh daily selection incrementally whenever a new part finishes audio or run completes
      if (
        (curatorStatus.running && curatorStatus.parts_ready !== prevReady) ||
        (prevRunning && !curatorStatus.running)
      ) {
        await fetchCuratorDaily(curatorSelectedDate || '');
      }
    } catch (e) {
      console.warn('Curator status fetch error:', e);
    }
  }

  async function fetchCuratorDaily(dateStr = '') {
    try {
      const qs = dateStr ? `?date=${encodeURIComponent(dateStr)}` : '';
      const res = await fetch(`/api/curator/daily${qs}`);
      if (!res.ok) throw new Error('Failed to load curator daily');
      const data = await res.json();
      curatorDates = data.dates || [];
      curatorSelectedDate = data.date || '';
      curatorDaily = data.daily || null;
      curatorResearchLog = data.research_log || null;
      curatorStatus = data.status || null;

      renderCuratorDateSelect();
      renderCuratorStatusBanner(curatorStatus);
      renderCuratorDaily();
      renderCuratorResearchLog();
    } catch (err) {
      console.warn('Curator daily fetch error:', err);
    }
  }

  function renderCuratorDateSelect() {
    if (!curatorDateSelect) return;
    if (curatorDates.length === 0) {
      curatorDateSelect.innerHTML = '<option value="">データなし</option>';
      return;
    }
    curatorDateSelect.innerHTML = curatorDates
      .map((d) => `<option value="${escapeHtml(d)}" ${d === curatorSelectedDate ? 'selected' : ''}>${escapeHtml(d)}</option>`)
      .join('');
  }

  function getCuratorBadgeLabel(part) {
    const pType = part.part_type || 'core';
    const baseLabels = {
      core: 'Core',
      discovery: 'Discovery',
      wildcard: 'Wild Card',
      connections: 'Connections (Archive)',
    };
    const base = baseLabels[pType] || pType;
    const pid = part.part_id || '';
    const m = pid.match(/_(\d+)$/);
    return m ? `${base} #${m[1]}` : base;
  }

  function renderCuratorDaily() {
    if (!curatorDailyList) return;
    if (!curatorDaily || !Array.isArray(curatorDaily.parts) || curatorDaily.parts.length === 0) {
      curatorDailyList.innerHTML = `
        <div class="queue-empty">
          <p class="queue-empty-title">キュレーション結果がありません</p>
          <p>「今すぐ実行」を押すか、毎日 03:00 の自動生成をお待ちください。</p>
        </div>
      `;
      return;
    }

    curatorDailyList.innerHTML = '';

    // Compact daily lineup summary bar (supports 9-part, 6-part, and legacy 3-part JSON)
    const parts = curatorDaily.parts;
    const totalCount = parts.length;
    const readyCount = parts.filter((p) => p.audio_status === 'ready').length;
    const coreCount = parts.filter((p) => p.part_type === 'core').length;
    const discCount = parts.filter((p) => p.part_type === 'discovery').length;
    const wcCount = parts.filter((p) => p.part_type === 'wildcard').length;
    const summaryBar = document.createElement('div');
    summaryBar.className = 'curator-lineup-summary';
    summaryBar.innerHTML = `
      <span class="curator-lineup-count">📋 ${totalCount} 本選出（音声準備完了 ${readyCount}/${totalCount}）</span>
      <div class="curator-lineup-pills">
        ${coreCount > 0 ? `<span class="curator-lineup-pill core">Core ×${coreCount}</span>` : ''}
        ${discCount > 0 ? `<span class="curator-lineup-pill discovery">Discovery ×${discCount}</span>` : ''}
        ${wcCount > 0 ? `<span class="curator-lineup-pill wildcard">Wild Card ×${wcCount}</span>` : ''}
      </div>
    `;
    curatorDailyList.appendChild(summaryBar);

    if (curatorDaily.shortfall_warning) {
      const warn = document.createElement('div');
      warn.className = 'curator-status-banner';
      warn.style.borderColor = 'rgba(255,51,102,0.45)';
      warn.innerHTML = `<span style="font-size:0.78rem;color:#fda4af;">⚠️ ${escapeHtml(curatorDaily.shortfall_warning)}</span>`;
      curatorDailyList.appendChild(warn);
    }

    parts.forEach((part) => {
      const card = document.createElement('article');
      card.className = 'curator-card';

      const pType = part.part_type || 'core';
      const badgeText = getCuratorBadgeLabel(part);

      // Audio status pill
      const aStatus = part.audio_status || 'pending';
      let audioBadgeHtml = `<span class="curator-audio-status pending">⏳ 待機中</span>`;
      if (aStatus === 'ready') {
        const durText = part.audio_duration_sec ? ` (${formatTime(part.audio_duration_sec)})` : '';
        audioBadgeHtml = `<span class="curator-audio-status ready">🎧 準備完了${durText}</span>`;
      } else if (aStatus === 'synthesizing') {
        audioBadgeHtml = `<span class="curator-audio-status synthesizing">⏳ 合成中...</span>`;
      } else if (aStatus === 'failed') {
        audioBadgeHtml = `<span class="curator-audio-status failed">⚠️ 合成失敗</span>`;
      } else if (aStatus === 'expired') {
        audioBadgeHtml = `<span class="curator-audio-status expired">期限切れ(再合成可)</span>`;
      }

      const engineBadgeHtml = renderEngineBadge(part.tts_engine);
      const estHtml = `<span class="badge-duration">約${part.estimated_minutes || '?'}分</span>`;

      // Source metadata
      const src = part.source || {};
      const accessLabel =
        src.access_type === 'free_full_text'
          ? '無料全文検証済'
          : src.access_type === 'open_abstract'
          ? '公開研究概要'
          : src.access_type === 'synthesis'
          ? '横断統合'
          : '検証済';

      const sourceHtml = `
        <div class="curator-source-meta">
          <span class="curator-free-badge">✓ ${escapeHtml(accessLabel)}</span>
          <span>📰 ${escapeHtml(src.source_name || '不明')}</span>
          ${src.author && src.author !== '記載なし' ? `<span>✍️ ${escapeHtml(src.author)}</span>` : ''}
          ${src.published_at && src.published_at !== '記載なし' ? `<span>📅 ${escapeHtml(src.published_at)}</span>` : ''}
          ${src.url ? `<a href="${escapeHtml(src.url)}" target="_blank" rel="noopener" class="curator-source-link">🔗 原文を開く</a>` : ''}
        </div>
      `;

      // 7-axis score box (if present)
      let scoresHtml = '';
      if (part.scores && Object.keys(part.scores).length > 0) {
        const axesPills = Object.keys(AXIS_LABELS)
          .map((k) => {
            const v = part.scores[k] !== undefined ? Number(part.scores[k]).toFixed(1) : '-';
            return `<div class="curator-axis-pill"><span>${AXIS_LABELS[k]}</span><strong>${v}</strong></div>`;
          })
          .join('');
        scoresHtml = `
          <div class="curator-scores-box">
            <div class="curator-score-headline">
              <span>7軸加重スコア: ${part.weighted_score !== null ? Number(part.weighted_score).toFixed(2) : '-'} / 5.00</span>
              <span style="color:var(--text-muted);font-weight:600;">適合 ${part.interest_fit ?? '-'} / 新規 ${part.novelty ?? '-'}</span>
            </div>
            <div class="curator-axes-grid">${axesPills}</div>
          </div>
        `;
      }

      // Enqueue button
      const isEnqueued = !!part.enqueued;
      const enqueueBtnLabel = isEnqueued
        ? '✓ キュー追加済（再追加可）'
        : aStatus === 'ready'
        ? '＋ キューへ追加（即再生）'
        : '＋ キューへ追加（音声生成）';

      // Feedback initial state
      const existingFb = part.feedback || {};
      const selectedRating = existingFb.rating || 0;
      const selectedReasons = new Set(existingFb.reasons || []);
      const existingMemo = existingFb.memo || '';

      const ratingBtnsHtml = CURATOR_RATINGS.map(
        (r) =>
          `<button type="button" class="curator-rating-btn ${selectedRating === r.val ? 'selected' : ''}" data-rating="${r.val}">${escapeHtml(r.label)}</button>`
      ).join('');

      const reasonChipsHtml = CURATOR_REASON_CHIPS.map(
        (chip) =>
          `<button type="button" class="curator-reason-chip ${selectedReasons.has(chip) ? 'selected' : ''}" data-reason="${escapeHtml(chip)}">${escapeHtml(chip)}</button>`
      ).join('');

      card.innerHTML = `
        <div class="curator-card-head">
          <span class="curator-part-badge ${escapeHtml(pType)}">${escapeHtml(badgeText)}</span>
          <div class="curator-audio-badges">
            ${engineBadgeHtml}
            ${estHtml}
            ${audioBadgeHtml}
          </div>
        </div>
        <h3 class="curator-title">${escapeHtml(part.title)}</h3>
        <div class="curator-why">${escapeHtml(part.why_selected || '')}</div>
        ${sourceHtml}
        ${scoresHtml}
        <div class="curator-action-row">
          <button type="button" class="btn-curator-enqueue ${isEnqueued ? 'enqueued' : ''}" data-action="enqueue">
            ${enqueueBtnLabel}
          </button>
          <button type="button" class="btn-curator-matrix ${part.sent_to_matrix_note ? 'sent' : ''}" data-action="curator-matrix">
            ${part.sent_to_matrix_note ? '✅ Matrix済' : '📝 Matrix Note'}
          </button>
        </div>
        <div class="curator-feedback-box">
          <div class="curator-fb-title">📝 フィードバック（Core 推薦の学習に反映 / Wild Card には非適用）</div>
          <div class="curator-rating-row">${ratingBtnsHtml}</div>
          <div class="curator-reason-row">${reasonChipsHtml}</div>
          <div class="curator-memo-row">
            <input type="text" class="curator-memo-input" placeholder="任意メモ（例：次回この視点を深掘りしたい）" value="${escapeHtml(existingMemo)}">
            <button type="button" class="btn-curator-fb-save">保存</button>
          </div>
        </div>
      `;

      // Wire enqueue button
      const enqueueBtn = card.querySelector('[data-action="enqueue"]');
      enqueueBtn.addEventListener('click', async () => {
        enqueueBtn.disabled = true;
        enqueueBtn.textContent = '⏳ キューに追加中...';
        try {
          const res = await fetch('/api/curator/enqueue', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ date: curatorDaily.date, part_id: part.part_id }),
          });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail || 'キュー追加に失敗しました');
          }
          const data = await res.json();
          part.enqueued = true;
          enqueueBtn.classList.add('enqueued');
          enqueueBtn.textContent = '✓ キュー追加済（再追加可）';
          showToast(
            data.instant_ready
              ? '🎧 事前音声化済みMP3を即座にキューへ追加しました！（待ち時間0秒）'
              : '⏳ キューに追加しました。バックグラウンドで音声合成します。'
          );
          await fetchQueue();
        } catch (err) {
          showToast(`エラー: ${err.message}`);
          enqueueBtn.textContent = enqueueBtnLabel;
        } finally {
          enqueueBtn.disabled = false;
        }
      });

      // Wire curator Matrix Note button
      const curatorMatrixBtn = card.querySelector('[data-action="curator-matrix"]');
      if (curatorMatrixBtn) {
        curatorMatrixBtn.addEventListener('click', async () => {
          if (part.sent_to_matrix_note) {
            showToast('すでに Matrix Note へ送信済みです');
            return;
          }
          curatorMatrixBtn.disabled = true;
          curatorMatrixBtn.textContent = '⏳ 送信中...';
          try {
            const res = await fetch('/api/curator/send-to-matrix-note', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ date: curatorDaily.date, part_id: part.part_id, force: false }),
            });
            if (!res.ok) {
              const err = await res.json().catch(() => ({}));
              throw new Error(err.detail || 'Matrix Note 送信に失敗しました');
            }
            const data = await res.json();
            part.sent_to_matrix_note = true;
            part.matrix_note_id = data.note_id || part.matrix_note_id;
            curatorMatrixBtn.classList.add('sent');
            curatorMatrixBtn.textContent = '✅ Matrix済';
            showToast(data.already_sent ? 'すでに Matrix Note へ送信済みです' : '📝 Matrix Note へメモを送信しました！');
          } catch (err) {
            showToast(`エラー: ${err.message}`);
            curatorMatrixBtn.disabled = false;
            curatorMatrixBtn.textContent = '📝 Matrix Note';
          }
        });
      }

      // Wire feedback controls
      let currentRating = selectedRating;
      const activeReasons = new Set(selectedReasons);
      const ratingBtns = card.querySelectorAll('.curator-rating-btn');
      ratingBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
          currentRating = Number(btn.dataset.rating);
          ratingBtns.forEach((b) => b.classList.toggle('selected', Number(b.dataset.rating) === currentRating));
        });
      });

      const reasonBtns = card.querySelectorAll('.curator-reason-chip');
      reasonBtns.forEach((btn) => {
        btn.addEventListener('click', () => {
          const r = btn.dataset.reason;
          if (activeReasons.has(r)) {
            activeReasons.delete(r);
            btn.classList.remove('selected');
          } else {
            activeReasons.add(r);
            btn.classList.add('selected');
          }
        });
      });

      const memoInput = card.querySelector('.curator-memo-input');
      const saveFbBtn = card.querySelector('.btn-curator-fb-save');
      saveFbBtn.addEventListener('click', async () => {
        if (!currentRating) {
          showToast('先に5段階評価を選択してください');
          return;
        }
        saveFbBtn.disabled = true;
        try {
          const res = await fetch('/api/curator/feedback', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              date: curatorDaily.date,
              part_id: part.part_id,
              rating: currentRating,
              reasons: Array.from(activeReasons),
              memo: memoInput.value.trim(),
            }),
          });
          if (!res.ok) throw new Error('フィードバック保存に失敗しました');
          showToast('✓ フィードバックを保存し、Core学習プロファイルを更新しました');
        } catch (err) {
          showToast(`エラー: ${err.message}`);
        } finally {
          saveFbBtn.disabled = false;
        }
      });

      curatorDailyList.appendChild(card);
    });
  }

  function renderCuratorResearchLog() {
    if (!curatorLogSummary || !curatorLogList) return;
    if (!curatorResearchLog) {
      curatorLogSummary.innerHTML = '<p style="color:var(--text-muted);">Research Log はまだありません。</p>';
      curatorLogList.innerHTML = '';
      return;
    }

    const stats = curatorResearchLog.stats || {};
    const newSources = curatorResearchLog.new_sources_discovered || [];
    const nextQs = curatorResearchLog.next_research_questions || [];

    curatorLogSummary.innerHTML = `
      <div class="log-stats-row">
        <span class="log-stat-pill">収集: <strong>${stats.raw_collected ?? 0}</strong></span>
        <span class="log-stat-pill">検証通過: <strong>${stats.verified_count ?? 0}</strong></span>
        <span class="log-stat-pill">除外: <strong>${stats.rejected_count ?? 0}</strong></span>
      </div>
      ${
        newSources.length > 0
          ? `<div>
               <div class="log-subblock-title">🌐 新規発見ソース</div>
               <ul class="log-bullet-list">${newSources.map((s) => `<li>${escapeHtml(s)}</li>`).join('')}</ul>
             </div>`
          : ''
      }
      ${
        nextQs.length > 0
          ? `<div>
               <div class="log-subblock-title">💡 次回調査したい問い</div>
               <ul class="log-bullet-list">${nextQs.map((q) => `<li>${escapeHtml(q)}</li>`).join('')}</ul>
             </div>`
          : ''
      }
    `;

    const cands = curatorResearchLog.candidates || [];
    curatorLogList.innerHTML = '';
    cands.forEach((c) => {
      const card = document.createElement('div');
      card.className = 'log-candidate-card';
      const st = c.status || 'rejected';
      let badgeCls = 'rejected';
      let badgeLabel = '除外';
      if (st.startsWith('selected:')) {
        badgeCls = 'selected';
        const slotRaw = st.split(':')[1] || '';
        const formattedSlot = slotRaw
          .replace(/^wildcard/, 'wild card')
          .replace(/_(\d+)$/, ' #$1')
          .toUpperCase();
        badgeLabel = `採用: ${formattedSlot}`;
      } else if (st === 'not_selected') {
        badgeCls = 'not_selected';
        badgeLabel = `候補（スコア ${c.weighted_score ?? '-'}）`;
      }

      const canPick = !st.startsWith('selected:') && c.url && c.url.startsWith('http');
      card.innerHTML = `
        <div class="log-candidate-top">
          <span class="log-status-badge ${badgeCls}">${escapeHtml(badgeLabel)}</span>
          <span style="font-size:0.72rem;color:var(--text-muted);">${escapeHtml(c.source_name || '')}</span>
        </div>
        <div class="log-cand-title">${escapeHtml(c.title || '無題')}</div>
        <div class="log-cand-reason">${escapeHtml(c.reason || '')}</div>
        <div class="log-cand-actions">
          ${c.url ? `<a href="${escapeHtml(c.url)}" target="_blank" rel="noopener" class="curator-source-link">🔗 元記事</a>` : '<span></span>'}
          ${canPick ? `<button type="button" class="btn-pick-candidate">＋ 自分で聴く</button>` : ''}
        </div>
      `;

      const pickBtn = card.querySelector('.btn-pick-candidate');
      if (pickBtn) {
        pickBtn.addEventListener('click', async () => {
          pickBtn.disabled = true;
          pickBtn.textContent = '⏳ 投入中...';
          try {
            const res = await fetch('/api/curator/pick-candidate', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ url: c.url, title: c.title, date: curatorSelectedDate }),
            });
            if (!res.ok) throw new Error('投入に失敗しました');
            pickBtn.textContent = '✓ キュー投入済';
            showToast('🎧 再生キューに投入しました（音声化を開始します）');
            await fetchQueue();
          } catch (err) {
            showToast(`エラー: ${err.message}`);
            pickBtn.disabled = false;
            pickBtn.textContent = '＋ 自分で聴く';
          }
        });
      }

      curatorLogList.appendChild(card);
    });
  }

  // -------------------------------------------------------------
  // Initialization & Polling loop for background queue processing
  // -------------------------------------------------------------
  setupBookmarklet();
  setupPdfUpload();
  fetchQueue();
  setInterval(() => {
    const hasActiveTask = queue.some(it => it.status === 'pending' || it.status === 'processing');
    if (hasActiveTask || document.visibilityState === 'visible') {
      fetchQueue();
    }
    if (activeTab === 'curator' || (curatorStatus && curatorStatus.running)) {
      fetchCuratorStatus();
    }
  }, 4000);

})();
