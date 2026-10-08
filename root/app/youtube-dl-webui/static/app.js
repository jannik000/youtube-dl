// Log polling for the dashboard. Served as a static file so the CSP can be
// script-src 'self' with no inline scripts. All parameters come from data
// attributes on #log-config, never interpolated into script source.
(function () {
  'use strict';
  var cfg = document.getElementById('log-config');
  if (!cfg) { return; }
  var basePath = cfg.getAttribute('data-base-path') || '';
  var downloadId = cfg.getAttribute('data-download-id') || '';
  var url = downloadId
    ? basePath + '/log/download/' + encodeURIComponent(downloadId)
    : basePath + '/log/youtube-dl';
  var el = document.getElementById('text');
  if (!el) { return; }

  function fetchLog() {
    fetch(url)
      .then(function (response) { return response.text(); })
      .then(function (data) {
        var atBottom = el.scrollHeight - el.scrollTop === el.clientHeight;
        el.textContent = data;
        if (atBottom) { el.scrollTop = el.scrollHeight; }
      })
      .catch(function () { /* transient errors are ignored; next tick retries */ });
  }

  setInterval(fetchLog, 2000);
  fetchLog();
})();
