/* FinTracker Android capability spike — mockup interaction.
   Screen router: one screen visible at a time; the step rail is generated from the DOM.
   Provenance markers: every .scr carries data-state="fresh|stale|offline|expired|demo|masked|..."
   and at least one visible [data-state] element (chip or banner) inside it. */
(function () {
  var screens = Array.prototype.slice.call(document.querySelectorAll('.scr'));
  var rail = document.getElementById('steps');
  var noteTitle = document.getElementById('noteTitle');
  var noteBody = document.getElementById('noteBody');
  var probe = document.getElementById('probe');
  var current = null;
  var backTarget = null;

  function screenById(id) {
    for (var i = 0; i < screens.length; i++) if (screens[i].id === id) return screens[i];
    return null;
  }

  function toast(msg) {
    var host = document.querySelector('.screen') || document.body;
    var t = document.createElement('div');
    t.className = 'toast';
    t.setAttribute('role', 'status');
    t.textContent = msg;
    host.appendChild(t);
    setTimeout(function () { t.remove(); }, 2400);
  }

  /* ---- step rail, built from the DOM so screens and rail never drift ---- */
  var index = 1;
  screens.forEach(function (s) {
    if (s.hasAttribute('data-norail') || !rail) return;
    var li = document.createElement('li');
    var b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('data-go', s.id);
    var num = document.createElement('span');
    num.className = 'num';
    num.textContent = s.hasAttribute('data-branch') ? '↳' : String(index++);
    var lbl = document.createElement('span');
    lbl.textContent = s.getAttribute('data-rail') || s.id;
    b.appendChild(num);
    b.appendChild(lbl);
    li.appendChild(b);
    rail.appendChild(li);
  });

  function go(id) {
    var next = screenById(id);
    if (!next) return;
    current = id;
    screens.forEach(function (s) {
      s.hidden = s !== next;
    });
    if (rail) {
      Array.prototype.forEach.call(rail.querySelectorAll('button'), function (b) {
        b.classList.toggle('active', b.getAttribute('data-go') === id);
      });
    }
    if (noteTitle) noteTitle.textContent = next.getAttribute('data-title') || '';
    if (noteBody) noteBody.innerHTML = next.getAttribute('data-note') || '';
    if (probe) {
      var p = next.getAttribute('data-probe');
      probe.innerHTML = p ? '<b>Spike probe</b><br>' + p : '';
      probe.hidden = !p;
    }
    var back = next.getAttribute('data-back');
    backTarget = back || null;
    var pill = next.querySelector('.gesturebar');
    if (pill) {
      pill.classList.toggle('root', !back);
      pill.setAttribute('aria-label', back
        ? 'Android system back → ' + (screenById(back) ? (screenById(back).getAttribute('data-rail') || back) : back)
        : 'Android system back — no in-app destination, would leave the app');
    }
    next.scrollTop = 0;
    var content = next.querySelector('.content');
    if (content) content.scrollTop = 0;
  }

  /* ---- click delegation: in-screen branch buttons + rail + inline toasts ---- */
  document.addEventListener('click', function (e) {
    var toastEl = e.target.closest('[data-toast]');
    if (toastEl) {
      toast(toastEl.getAttribute('data-toast'));
      if (!toastEl.hasAttribute('data-go')) return;
    }
    var goEl = e.target.closest('[data-go]');
    if (goEl) { go(goEl.getAttribute('data-go')); return; }
    var pill = e.target.closest('.gesturebar');
    if (pill) {
      if (!backTarget) { toast('No in-app destination — system back would leave the app.'); return; }
      go(backTarget);
    }
  });

  /* ---- dark mode toggle ---- */
  var modeBtn = document.getElementById('modeToggle');
  if (modeBtn) {
    modeBtn.addEventListener('click', function () {
      document.body.classList.toggle('dark');
    });
  }

  /* ---- boot ---- */
  var first = document.querySelector('.scr');
  if (first) go(first.id);
})();
