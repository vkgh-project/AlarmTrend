// Read only local data. Browser visits never initiate source synchronization.
const dashboard = document.querySelector('#dashboard');
if (dashboard) {
  setInterval(async () => {
    if (document.querySelector('input:focus')) return;
    try {
      const response = await fetch('/api/status', {cache: 'no-store'});
      if (!response.ok) return;
      const data = await response.json();
      if (data.at !== dashboard.dataset.at || String(data.stale || data.sync_error || data.forecast_stale) !== dashboard.dataset.problem) location.reload();
    } catch (_) {
      const banner = document.querySelector('#connection-warning');
      if (banner) banner.hidden = false;
    }
  }, 60000);
}
