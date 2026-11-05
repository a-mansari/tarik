(function () {
  var form = document.getElementById('form');
  var steps = [].slice.call(form.querySelectorAll('fieldset.step'));
  var recap = document.getElementById('recap');
  var list = document.getElementById('list');
  var prev = document.getElementById('prev');
  var next = document.getElementById('next');
  var fill = document.getElementById('fill');
  var count = document.getElementById('count');
  var at = 0;
  document.documentElement.classList.add('js');

  function said(step) {
    var out = [];
    [].slice.call(step.querySelectorAll('.card')).forEach(function (card) {
      var picked = [].slice.call(card.querySelectorAll('.opt input:checked')).map(function (el) {
        var row = el.closest('.opt');
        var other = row.querySelector('.other-in');
        var label = row.querySelector('b').textContent.trim();
        return other && other.value ? label + ' : ' + other.value : label;
      });
      [].slice.call(card.querySelectorAll('input[type=text],input:disabled')).forEach(function (el) {
        if (el.value && !el.classList.contains('other-in')) picked.push(el.value);
      });
      var note = card.querySelector('textarea');
      out.push({ q: card.querySelector('h3, .field').textContent.trim(), a: picked.join(' ; '), note: note && note.value });
    });
    return out;
  }

  function build() {
    list.textContent = '';
    steps.forEach(function (step, n) {
      said(step).forEach(function (row) {
        var box = document.createElement('div'); box.className = 'rc';
        var body = document.createElement('div');
        body.appendChild(document.createTextNode(row.q));
        var small = document.createElement('small');
        small.textContent = row.a || 'Pas de réponse';
        body.appendChild(small);
        if (row.note) { var more = document.createElement('small'); more.textContent = 'Précision : ' + row.note; body.appendChild(more); }
        var edit = document.createElement('button'); edit.type = 'button'; edit.className = 'ghost'; edit.textContent = 'Modifier';
        edit.addEventListener('click', function () { show(n); });
        box.appendChild(body); box.appendChild(edit); list.appendChild(box);
      });
    });
  }

  function show(n) {
    at = Math.max(0, Math.min(steps.length, n));
    var last = at === steps.length;
    steps.forEach(function (s, k) { s.classList.toggle('active', k === at); });
    recap.classList.toggle('active', last);
    document.documentElement.classList.toggle('final', last);
    if (last) build();
    prev.disabled = at === 0;
    next.textContent = at === steps.length - 1 ? 'Voir le récapitulatif →' : 'Suivant →';
    fill.style.width = ((last ? steps.length : at + 1) / steps.length * 100) + '%';
    count.textContent = last ? 'Récapitulatif' : 'Étape ' + (at + 1) + ' / ' + steps.length;
    try { window.scrollTo({ top: 0 }); } catch (e) {}
  }

  prev.addEventListener('click', function () { show(at - 1); });
  next.addEventListener('click', function () { show(at + 1); });
  form.addEventListener('keydown', function (e) {
    // Enter on any field goes to the next step, never sends the form (a recap's own button does)
    if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); if (at < steps.length) show(at + 1); }
  });
  try {
    show(form.hasAttribute('data-error') ? steps.length : 0);  // after a refusal, the recap: every answer, each editable
  } catch (e) {
    document.documentElement.classList.remove('js');  // no wizard, but the whole form is there
  }
})();
