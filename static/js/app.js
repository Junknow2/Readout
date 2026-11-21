// Readout - Running Optimized Audio PWA Client
(() => {
  'use strict';

  // State
  let queue = [];
  let currentIndex = -1;
  let isPlaying = false;
  let cachedAudioUrls = new Set();
  const speeds = [1.0, 1.25, 1.5, 2.0];
  let currentSpeedIdx = 0;

  // DOM Elements
  const audio = document.getElementById('audio-player');
  const networkBadge = document.getElementById('network-badge');
  const urlInput = document.getElementById('url-input');
  const btnAddUrl = document.getElementById('btn-add-url');
  const btnRss = document.getElementById('btn-rss');
  const queueList = document.getElementById('queue-list');
  const queueCounter = document.getElementById('queue-counter');
  const toast = document.getElementById('toast');

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
            // Ask SW to pre-cache
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
  // API Calls
  // -------------------------------------------------------------
  async function fetchQueue() {
    try {
      const res = await fetch('/api/queue');
      if (!res.ok) throw new Error('Failed to fetch queue');
      const data = await res.json();
      
      const prevLength = queue.length;
      queue = data;
      renderQueue();
      checkOfflineCache();

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
      // If current item was listened, move to next
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
    isPlaying = false;
    updatePlayPauseUI();
  }

  function togglePlayPause() {
    if (currentIndex === -1) {
      const firstReady = queue.findIndex(it => it.status === 'ready');
      if (firstReady !== -1) {
        loadTrack(firstReady, true);
      } else {
        showToast('再生可能なアイテムがありません');
      }
      return;
    }

    if (audio.paused) {
      audio.play().then(() => {
        isPlaying = true;
        updatePlayPauseUI();
      }).catch(console.error);
    } else {
      audio.pause();
      isPlaying = false;
      updatePlayPauseUI();
    }
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

  function playNext() {
    if (queue.length === 0) return;
    // Find next ready track
    for (let i = currentIndex + 1; i < queue.length; i++) {
      if (queue[i].status === 'ready') {
        loadTrack(i, true);
        return;
      }
    }
    showToast('キューの最後のトラックです');
  }

  function playPrev() {
    if (queue.length === 0) return;
    // If > 3 seconds in, rewind to start
    if (audio.currentTime > 3) {
      audio.currentTime = 0;
      return;
    }
    // Find previous ready track
    for (let i = currentIndex - 1; i >= 0; i--) {
      if (queue[i].status === 'ready') {
        loadTrack(i, true);
        return;
      }
    }
    audio.currentTime = 0;
  }

  function seekRelative(delta) {
    if (isNaN(audio.duration)) return;
    const newTime = Math.max(0, Math.min(audio.duration, audio.currentTime + delta));
    audio.currentTime = newTime;
    updateProgressUI();
  }

  function cycleSpeed() {
    currentSpeedIdx = (currentSpeedIdx + 1) % speeds.length;
    const speed = speeds[currentSpeedIdx];
    audio.playbackRate = speed;
    btnSpeed.textContent = `${speed.toFixed(speed % 1 === 0 ? 1 : 2)}x`;
    showToast(`再生速度: ${speed}x`);
  }

  function updateProgressUI() {
    if (isNaN(audio.duration) || audio.duration === 0) {
      progressFill.style.width = '0%';
      timeCurrent.textContent = '0:00';
      return;
    }
    const percent = (audio.currentTime / audio.duration) * 100;
    progressFill.style.width = `${percent}%`;
    timeCurrent.textContent = formatTime(audio.currentTime);
    timeDuration.textContent = formatTime(audio.duration);
  }

  // Audio Event Listeners
  audio.addEventListener('timeupdate', () => {
    updateProgressUI();
    if ('mediaSession' in navigator && !isNaN(audio.duration)) {
      try {
        navigator.mediaSession.setPositionState({
          duration: audio.duration,
          playbackRate: audio.playbackRate,
          position: audio.currentTime
        });
      } catch (e) {}
    }
  });

  audio.addEventListener('play', () => {
    isPlaying = true;
    updatePlayPauseUI();
  });

  audio.addEventListener('pause', () => {
    isPlaying = false;
    updatePlayPauseUI();
  });

  audio.addEventListener('ended', async () => {
    // Current track ended!
    showToast('再生が終了しました');
    if (currentIndex !== -1 && queue[currentIndex]) {
      const endedItemId = queue[currentIndex].id;
      // Mark as listened in backend
      markListened(endedItemId);
    }
  });

  // Seekbar click handler
  progressWrap.addEventListener('click', (e) => {
    if (isNaN(audio.duration) || audio.duration === 0) return;
    const rect = progressWrap.getBoundingClientRect();
    const clickX = e.clientX - rect.left;
    const fraction = Math.max(0, Math.min(1, clickX / rect.width));
    audio.currentTime = fraction * audio.duration;
  });

  // -------------------------------------------------------------
  // MediaSession API Integration (Lockscreen & Shokz/Bluetooth Controls)
  // -------------------------------------------------------------
  function updateMediaSession(item) {
    if (!('mediaSession' in navigator)) return;

    navigator.mediaSession.metadata = new MediaMetadata({
      title: item.title,
      artist: 'Readout',
      album: 'マラソン・ランニング 音声キュー',
      artwork: [
        { src: '/static/icons/icon-192.png', sizes: '192x192', type: 'image/png' },
        { src: '/static/icons/icon-512.png', sizes: '512x512', type: 'image/png' }
      ]
    });

    navigator.mediaSession.setActionHandler('play', () => {
      audio.play();
    });
    navigator.mediaSession.setActionHandler('pause', () => {
      audio.pause();
    });
    navigator.mediaSession.setActionHandler('seekbackward', (details) => {
      seekRelative(-(details.seekOffset || 10));
    });
    navigator.mediaSession.setActionHandler('seekforward', (details) => {
      seekRelative(details.seekOffset || 30);
    });
    navigator.mediaSession.setActionHandler('previoustrack', () => {
      playPrev();
    });
    navigator.mediaSession.setActionHandler('nexttrack', () => {
      playNext();
    });
    try {
      navigator.mediaSession.setActionHandler('seekto', (details) => {
        if (details.seekTime !== null && details.seekTime !== undefined) {
          audio.currentTime = details.seekTime;
        }
      });
    } catch (e) {}
  }

  // -------------------------------------------------------------
  // Render Queue UI
  // -------------------------------------------------------------
  function renderQueue() {
    queueCounter.textContent = `${queue.length} 件`;

    if (queue.length === 0) {
      queueList.innerHTML = `
        <div class="queue-empty">
          <p class="queue-empty-title">キューは空です</p>
          <p>上の入力欄に読みたい記事のURLを入れてください。</p>
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
          <h3 class="card-title">${item.title}</h3>
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
  // Polling loop for background queue processing
  // -------------------------------------------------------------
  fetchQueue();
  setInterval(() => {
    // If there's any pending/processing item, poll frequently
    const hasActiveTask = queue.some(it => it.status === 'pending' || it.status === 'processing');
    if (hasActiveTask || document.visibilityState === 'visible') {
      fetchQueue();
    }
  }, 4000);

})();
