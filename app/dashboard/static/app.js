(function () {
  // progress bars: widths are set here because the CSP forbids inline style attributes
  document.querySelectorAll('[data-pct]').forEach(function (el) {
    el.style.width = el.getAttribute('data-pct') + '%';
  });

  var dataEl = document.getElementById('chart-data');
  if (!dataEl || typeof Chart === 'undefined') return;
  var d = JSON.parse(dataEl.textContent);

  var fa = function (n) { return n == null ? '' : Number(n).toLocaleString('fa-IR'); };
  var faPct = function (n) { return n == null ? '' : Number(n).toLocaleString('fa-IR', { maximumFractionDigits: 1 }) + '٪'; };
  var palette = ['#3b6ef5', '#1f9d63', '#e0961a', '#8a5cf6', '#d64545', '#14a3b8', '#7a869a'];

  Chart.defaults.font.family = 'Vazirmatn, Tahoma, "Segoe UI", system-ui, sans-serif';
  Chart.defaults.color = '#66728a';
  Chart.defaults.maintainAspectRatio = false;
  Chart.defaults.plugins.legend.rtl = true;
  Chart.defaults.plugins.legend.labels.usePointStyle = true;
  Chart.defaults.plugins.tooltip.rtl = true;
  Chart.defaults.plugins.tooltip.textDirection = 'rtl';

  var tooltipTitle = function (items) { return d.isoLabels[items[0].dataIndex]; };
  var axes = function (yFormat, stacked) {
    return {
      x: { stacked: !!stacked, grid: { display: false }, ticks: { maxTicksLimit: 10 } },
      y: { stacked: !!stacked, beginAtZero: true, ticks: { callback: function (v) { return yFormat(v); } } }
    };
  };
  var make = function (id, config) {
    var el = document.getElementById(id);
    if (el) new Chart(el, config);
  };
  var bars = function (id, series, yFormat) {
    var names = Object.keys(series);
    make(id, {
      type: 'bar',
      data: {
        labels: d.labels,
        datasets: names.map(function (n, i) { return { label: n, data: series[n], backgroundColor: palette[i % palette.length], borderRadius: 3 }; })
      },
      options: { scales: axes(yFormat, true), plugins: { tooltip: { callbacks: { title: tooltipTitle, label: function (c) { return c.dataset.label + ': ' + yFormat(c.parsed.y); } } } } }
    });
  };
  var line = function (id, sets, yFormat, max) {
    var scales = axes(yFormat, false);
    if (max) scales.y.max = max;
    make(id, {
      type: 'line',
      data: {
        labels: d.labels,
        datasets: sets.map(function (s, i) {
          return { label: s.label, data: s.data, borderColor: palette[i % palette.length], backgroundColor: palette[i % palette.length], tension: 0.3, pointRadius: 2, spanGaps: true, borderWidth: 2 };
        })
      },
      options: { scales: scales, plugins: { tooltip: { callbacks: { title: tooltipTitle, label: function (c) { return c.dataset.label + ': ' + yFormat(c.parsed.y); } } } } }
    });
  };

  bars('c-tokens-model', d.tokensByModel, fa);
  bars('c-tokens-purpose', d.tokensByPurpose, fa);
  bars('c-cost', d.costByModel, function (v) { return '$' + Number(v).toLocaleString('fa-IR', { maximumFractionDigits: 4 }); });
  line('c-latency', [{ label: 'P50', data: d.latencyP50 }, { label: 'P95', data: d.latencyP95 }], function (v) { return fa(Math.round(v)) + ' م‌ث'; });
  var pct = function (arr) { return arr.map(function (x) { return x == null ? null : x * 100; }); };
  line('c-rates', [
    { label: 'موفقیت · Success', data: pct(d.successRate) },
    { label: 'تأیید · Confirmation', data: pct(d.confirmationRate) },
    { label: 'ویرایش · Edit', data: pct(d.editRate) }
  ], faPct, 100);

  make('c-expensive', {
    type: 'bar',
    data: {
      labels: d.expensive.map(function (r) { return '#' + r.id; }),
      datasets: [{ label: 'USD', data: d.expensive.map(function (r) { return r.cost; }), backgroundColor: '#3b6ef5', borderRadius: 3 }]
    },
    options: {
      indexAxis: 'y',
      plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return '$' + Number(c.parsed.x).toLocaleString('fa-IR', { maximumFractionDigits: 4 }) + ' · ' + d.expensive[c.dataIndex].model; } } } },
      scales: { x: { beginAtZero: true, ticks: { callback: function (v) { return '$' + Number(v).toLocaleString('fa-IR'); } } }, y: { grid: { display: false } } },
      onClick: function (evt, els) { if (els.length) window.location = (document.body.getAttribute('data-base') || '') + '/runs/' + d.expensive[els[0].index].id; }
    }
  });
})();
