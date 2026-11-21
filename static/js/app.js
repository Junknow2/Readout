// Readout - Running Optimized Audio PWA Client
(() => {
  'use strict';

  // -------------------------------------------------------------
  // State
  // -------------------------------------------------------------
  let queue = [];
  let currentIndex = -1;
  let isPlaying = false;
  let cachedAudioUrls = new Set();
  const speeds = [1.0, 1.25, 1.5, 2.0];
  let currentSpeedIdx = 0;

  // RSS State
  let activeTab = 'queue';
  let feeds = [];
  let articles = [];
  let isLoadingArticles = false;
  let feedManagerOpen = false;

  // -------------------------------------------------------------
  // DOM Elements
  // -------------------------------------------------------------
  const audio = document.getElementById('audio-player');
  const networkBadge = document.getElementById('network-badge');
  const toast = document.getElementById('toast');
  const btnRss = document.getElementById('btn-rss');

  // Tabs DOM
  const tabBtnQueue = document.getElementById('tab-btn-queue');
  const tabBtnRss = document.getElementById('tab-btn-rss');
  const paneQueue = document.getElementById('pane-queue');
  const paneRss = document.getElementById('pane-rss');

  // Queue View DOM
  const urlInput = document.getElementById('url-input');
  const btnAddUrl = document.getElementById('btn-add-url');
  const queueList = document.getElementById('queue-list');
  const queueCounter = document.getElementById('queue-counter');

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

  // RSS View DOM
  const feedManagerToggle = document.getElementById('feed-manager-toggle');
  const feedManagerArrow = document.getElementById('feed-manager-arrow');
  const feedManagerBody = document.getElementById('feed-manager-body');
  const feedUrlInput = document.getElementById('feed-url-input');
  const btnAddFeed = document.getElementById('btn-add-feed');
  const feedChips = document.getElementById('feed-chips');
  const feedCount = document.getElementById('feed-count');
  const articlesCounter = document.getElementById('articles-counter');
  const btnRefreshArticles = document.getElementById('btn-refresh-articles');
  const articlesList = document.getElementById('articles-list');

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

  // -------------------------------------------------------------
  // Tab Switcher
  // -------------------------------------------------------------
  function switchTab(target) {
    if (target === activeTab) return;
    activeTab = target;

    if (target === 'queue') {
      tabBtnQueue.classList.add('active');
      tabBtnQueue.setAttribute('aria-selected', 'true');
      tabBtnRss.classList.remove('active');
      tabBtnRss.setAttribute('aria-selected', 'false');

      paneQueue.style.display = 'flex';
      paneRss.style.display = 'none';
    } else if (target === 'rss') {
      tabBtnRss.classList.add('active');
      tabBtnRss.setAttribute('aria-selected', 'true');
      tabBtnQueue.classList.remove('active');
      tabBtnQueue.setAttribute('aria-selected', 'false');

      paneQueue.style.display = 'none';
      paneRss.style.display = 'flex';

      // Load feeds and articles if not yet loaded
      if (feeds.length === 0) {
        fetchFeeds();
      }
      if (articles.length === 0) {
        fetchArticles();
      }
    }
  }

  tabBtnQueue.addEventListener('click', () => switchTab('queue'));
  tabBtnRss.addEventListener('click', () => switchTab('rss'));

  // -------------------------------------------------------------
  // Cache Verification & Auto Pre-caching
  // -------------------------------------------------------------
  async function checkOfflineCache() {
    if (!('caches' in window)) return;
    try {
      const cache = await caches.open('readout-v1');
      for (const item of queue) {
        if (item.status === 'ready' && item.audio_filename) {
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
  async function fetchQueue() {
    try {
      const res = await fetch('/api/queue');
      if (!res.ok) throw new Error('Failed to fetch queue');
      const data = await res.json();
      
      queue = data;
      renderQueue();
      checkOfflineCache();
      syncArticlesQueueState();

      // If nothing is playing and we have items, set up the first ready item
      if (currentIndex === -1 && queue.length > 0) {
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
      await fetchQueue();
    } catch (err) {
      alert(`エラー: ${err.message}`);
    }
  }

  async function markListened(itemId) {
    try {
      await fetch(`/api/queue/${itemId}/listened`, { method: 'POST' });
      showToast('既読としてキューから除外しました');
      if (currentIndex !== -1 && queue[currentIndex]?.id === itemId) {
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

  async function deleteItem(itemId) {
    if (!confirm('このアイテムを削除しますか？')) return;
    try {
      await fetch(`/api/queue/${itemId}`, { method: 'DELETE' });
      showToast('アイテムを削除しました');
      if (currentIndex !== -1 && queue[currentIndex]?.id === itemId) {
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
  function loadTrack(index, autoPlay = true) {
    if (index < 0 || index >= queue.length) return;
    const item = queue[index];
    if (item.status !== 'ready') {
      showToast('このアイテムはまだ生成中です');
      return;
    }

    currentIndex = index;
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
    audio.src = '';
    playerTitle.textContent = '再生待ちのアイテムがありません';
    playerSummary.textContent = 'URLを追加すると、Geminiがポッドキャスト風にリライトして高音質音声化します。';
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
      queueList.innerHTML = `
        <div class="queue-empty">
          <p class="queue-empty-title">キューは空です</p>
          <p>上の入力欄に読みたい記事のURLを入れるか、RSSリーダーから追加してください。</p>
        </div>
      `;
      return;
    }

    queueList.innerHTML = '';
    queue.forEach((item, index) => {
      const card = document.createElement('div');
      const isCurrent = index === currentIndex;
      const isProcessing = item.status === 'processing' || item.status === 'pending';
      const isReady = item.status === 'ready';
      const isCached = item.audio_filename && cachedAudioUrls.has(`/audio/${item.audio_filename}`);

      let cardClass = 'queue-card';
      if (isCurrent) cardClass += ' active-item';
      if (isProcessing) cardClass += ' processing-item';
      card.className = cardClass;

      // Status Badge
      let statusBadgeHtml = '';
      if (item.status === 'pending') {
        statusBadgeHtml = `<span class="badge-status processing">待機中</span>`;
      } else if (item.status === 'processing') {
        statusBadgeHtml = `<span class="badge-status processing">生成中...</span>`;
      } else if (item.status === 'failed') {
        statusBadgeHtml = `<span class="badge-status failed">生成失敗</span>`;
      }

      // Offline Badge
      let offlineBadgeHtml = '';
      if (isCached) {
        offlineBadgeHtml = `<span class="badge-offline">⚡ オフライン済</span>`;
      }

      // Duration Badge
      let durationHtml = '';
      if (item.audio_duration_sec > 0) {
        durationHtml = `<span class="badge-duration">${formatTime(item.audio_duration_sec)}</span>`;
      }

      card.innerHTML = `
        <div class="card-top">
          <h3 class="card-title">${escapeHtml(item.title)}</h3>
          <div class="card-meta">
            ${statusBadgeHtml}
            ${offlineBadgeHtml}
            ${durationHtml}
          </div>
        </div>
        <div class="card-actions">
          <button class="btn-card-action btn-listened" data-action="listened" title="聴き終わった（キューから除外）">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="20 6 9 17 4 12"/></svg>
            既読
          </button>
          <button class="btn-card-action btn-delete" data-action="delete" title="削除">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
            削除
          </button>
        </div>
      `;

      // Click card to play (if ready)
      card.addEventListener('click', (e) => {
        if (e.target.closest('.card-actions')) return;
        if (isReady) {
          loadTrack(index, true);
        } else if (isProcessing) {
          showToast('現在GeminiとTTSで音声を生成中です。少々お待ちください。');
        } else if (item.status === 'failed') {
          showToast(`生成エラー: ${item.error_message || '不明なエラー'}`);
        }
      });

      // Actions
      const btnListened = card.querySelector('[data-action="listened"]');
      btnListened.addEventListener('click', (e) => {
        e.stopPropagation();
        markListened(item.id);
      });

      const btnDelete = card.querySelector('[data-action="delete"]');
      btnDelete.addEventListener('click', (e) => {
        e.stopPropagation();
        deleteItem(item.id);
      });

      queueList.appendChild(card);
    });
  }

  // -------------------------------------------------------------
  // RSS Feeds Management
  // -------------------------------------------------------------
  feedManagerToggle.addEventListener('click', () => {
    feedManagerOpen = !feedManagerOpen;
    feedManagerBody.style.display = feedManagerOpen ? 'flex' : 'none';
    feedManagerArrow.classList.toggle('open', feedManagerOpen);
  });

  async function fetchFeeds() {
    try {
      const res = await fetch('/api/feeds');
      if (!res.ok) throw new Error('Failed to fetch feeds');
      feeds = await res.json();
      renderFeeds();
    } catch (err) {
      console.warn('Error fetching feeds:', err);
    }
  }

  function renderFeeds() {
    feedCount.textContent = `${feeds.length}`;
    feedChips.innerHTML = '';

    if (feeds.length === 0) {
      feedChips.innerHTML = '<span style="font-size: 0.8rem; color: #666;">購読フィードがありません</span>';
      return;
    }

    feeds.forEach((feed) => {
      const chip = document.createElement('div');
      chip.className = 'feed-chip';
      chip.innerHTML = `
        <span class="feed-chip-title" title="${escapeHtml(feed.url)}">${escapeHtml(feed.title)}</span>
        <button class="feed-chip-delete" data-id="${feed.id}" title="購読解除">×</button>
      `;

      const btnDeleteFeed = chip.querySelector('.feed-chip-delete');
      btnDeleteFeed.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (confirm(`「${feed.title}」の購読を解除しますか？`)) {
          await deleteFeed(feed.id);
        }
      });

      feedChips.appendChild(chip);
    });
  }

  async function addFeed(url) {
    try {
      showToast('フィードを登録中...');
      const res = await fetch('/api/feeds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: url })
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'フィード登録に失敗しました');
      }
      feedUrlInput.value = '';
      showToast('フィードを購読しました！');
      await fetchFeeds();
      await fetchArticles();
    } catch (err) {
      alert(`エラー: ${err.message}`);
    }
  }

  async function deleteFeed(feedId) {
    try {
      const res = await fetch(`/api/feeds/${feedId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error('フィード削除に失敗しました');
      showToast('購読を解除しました');
      await fetchFeeds();
      await fetchArticles();
    } catch (err) {
      alert(`エラー: ${err.message}`);
    }
  }

  btnAddFeed.addEventListener('click', () => {
    const url = feedUrlInput.value.trim();
    if (url) {
      addFeed(url);
    } else {
      showToast('フィードURLを入力してください');
    }
  });

  feedUrlInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      const url = feedUrlInput.value.trim();
      if (url) addFeed(url);
    }
  });

  // -------------------------------------------------------------
  // RSS Articles Timeline & 1-Tap Enqueue
  // -------------------------------------------------------------
  async function fetchArticles() {
    if (isLoadingArticles) return;
    isLoadingArticles = true;
    btnRefreshArticles.classList.add('spinning');
    articlesList.innerHTML = `
      <div class="articles-loading">
        <div class="spinner"></div>
        <p>最新記事をタイムラインに集約中...</p>
      </div>
    `;

    try {
      const res = await fetch('/api/articles');
      if (!res.ok) throw new Error('Failed to fetch articles');
      articles = await res.json();
      renderArticles();
    } catch (err) {
      console.error('Error fetching articles:', err);
      articlesList.innerHTML = `
        <div class="articles-empty">
          <p style="color: var(--accent-danger);">新着記事の取得に失敗しました</p>
          <button class="btn-icon-refresh" id="btn-retry-articles">再試行</button>
        </div>
      `;
      const retryBtn = document.getElementById('btn-retry-articles');
      if (retryBtn) retryBtn.addEventListener('click', fetchArticles);
    } finally {
      isLoadingArticles = false;
      btnRefreshArticles.classList.remove('spinning');
    }
  }

  btnRefreshArticles.addEventListener('click', fetchArticles);

  function syncArticlesQueueState() {
    if (articles.length === 0) return;
    const queuedUrls = new Set(queue.map(it => (it.url || '').trim().replace(/\/$/, '')));

    let hasChange = false;
    articles.forEach(art => {
      const cleanLink = (art.link || '').trim().replace(/\/$/, '');
      const isQueued = queuedUrls.has(cleanLink);
      if (art.in_queue !== isQueued) {
        art.in_queue = isQueued;
        hasChange = true;
      }
    });

    if (hasChange && activeTab === 'rss') {
      renderArticles();
    }
  }

  function renderArticles() {
    articlesCounter.textContent = `${articles.length} 件`;

    if (articles.length === 0) {
      articlesList.innerHTML = `
        <div class="articles-empty">
          <p>新着記事がありません。</p>
          <p style="font-size: 0.85rem;">フィード管理からお好みのテックRSS/Atomを追加してください。</p>
        </div>
      `;
      return;
    }

    articlesList.innerHTML = '';
    articles.forEach((art) => {
      const card = document.createElement('div');
      card.className = `article-card ${art.in_queue ? 'in-queue' : ''}`;

      const btnHtml = art.in_queue
        ? `<button class="btn-enqueue state-queued" disabled>
             <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>
             ✓ キュー追加済
           </button>`
        : `<button class="btn-enqueue" data-action="enqueue">
             <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
             🎧 走るリストに追加
           </button>`;

      card.innerHTML = `
        <div class="article-header">
          <span class="article-feed-badge" title="${escapeHtml(art.feed_title)}">${escapeHtml(art.feed_title)}</span>
          <span class="article-date">${escapeHtml(art.published)}</span>
        </div>
        <h3 class="article-title">${escapeHtml(art.title)}</h3>
        <p class="article-summary">${escapeHtml(art.summary || 'サマリーはありません')}</p>
        <div class="article-actions">
          ${btnHtml}
          <a href="${escapeHtml(art.link)}" target="_blank" rel="noopener" class="article-link">
            🔗 元記事を読む
          </a>
        </div>
      `;

      const enqueueBtn = card.querySelector('[data-action="enqueue"]');
      if (enqueueBtn) {
        enqueueBtn.addEventListener('click', () => {
          handleEnqueueArticle(art, enqueueBtn, card);
        });
      }

      articlesList.appendChild(card);
    });
  }

  async function handleEnqueueArticle(art, btn, card) {
    if (art.in_queue) return;

    btn.disabled = true;
    btn.className = 'btn-enqueue state-loading';
    btn.innerHTML = `
      <span class="spinner" style="width:12px;height:12px;border-width:2px;display:inline-block;vertical-align:middle;"></span>
      <span>⏳ 生成中...</span>
    `;

    try {
      const res = await fetch('/api/queue', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: art.link })
      });

      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || 'キュー追加に失敗しました');
      }

      art.in_queue = true;
      card.classList.add('in-queue');
      btn.className = 'btn-enqueue state-queued';
      btn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3"><polyline points="20 6 9 17 4 12"/></svg>
        <span>✓ キュー追加済</span>
      `;

      showToast('走るリストに追加しました！音声を生成しています...');
      await fetchQueue();
    } catch (err) {
      alert(`エラー: ${err.message}`);
      btn.disabled = false;
      btn.className = 'btn-enqueue';
      btn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M3 18v-6a9 9 0 0 1 18 0v6"/><path d="M21 19a2 2 0 0 1-2 2h-1a2 2 0 0 1-2-2v-3a2 2 0 0 1 2-2h3zM3 19a2 2 0 0 0 2 2h1a2 2 0 0 0 2-2v-3a2 2 0 0 0-2-2H3z"/></svg>
        <span>🎧 走るリストに追加</span>
      `;
    }
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
  // Initialization & Polling loop for background queue processing
  // -------------------------------------------------------------
  fetchQueue();
  setInterval(() => {
    const hasActiveTask = queue.some(it => it.status === 'pending' || it.status === 'processing');
    if (hasActiveTask || document.visibilityState === 'visible') {
      fetchQueue();
    }
  }, 4000);

})();
