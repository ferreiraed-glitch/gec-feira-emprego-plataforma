// Check-in na receção (7 Out 2026): procura por nome, grava sem recarregar a
// página e marca logo ao tocar nas caixas «Presente» e «Entrevista feita».
// Sem JavaScript, os formulários continuam a funcionar com «Guardar».
(function () {
  var semAcentos = function (s) { return s.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase(); };
  var filtro = document.querySelector('[data-filtro]');
  var pessoas = Array.prototype.slice.call(document.querySelectorAll('form.pessoa'));
  pessoas.forEach(function (f) { f.dataset.chave = semAcentos(f.dataset.nome || ''); });
  if (filtro) {
    filtro.addEventListener('input', function () {
      var q = semAcentos(filtro.value.trim());
      pessoas.forEach(function (f) { f.hidden = q !== '' && f.dataset.chave.indexOf(q) === -1; });
    });
  }

  function gravar(form) {
    var estado = form.querySelector('[data-estado]');
    estado.textContent = 'A guardar…';
    form.classList.remove('erro-gravar');
    fetch(form.action, { method: 'POST', body: new FormData(form), headers: { 'X-Pedido': 'fetch' },
                         credentials: 'same-origin' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok || !res.j.ok) { throw new Error(res.j.erro || 'erro'); }
        estado.textContent = 'Guardado às ' + res.j.hora;
        form.classList.remove('por-gravar');
        Object.keys(res.j.resumo).forEach(function (k) {
          var el = document.querySelector('[data-n="' + k + '"]');
          if (el) { el.textContent = res.j.resumo[k]; }
        });
      })
      .catch(function (e) {
        estado.textContent = 'Não gravou: ' + e.message + '. Tente de novo.';
        form.classList.add('erro-gravar');
      });
  }

  pessoas.forEach(function (form) {
    form.addEventListener('submit', function (ev) { ev.preventDefault(); gravar(form); });
    form.addEventListener('change', function (ev) {
      var alvo = ev.target;
      if (alvo.form !== form) { return; }       // campos da «Nova entrevista» não gravam aqui
      if (alvo.name === 'presente') {
        // Presente: as entrevistas ficam feitas por omissão (é mais fácil retirar quem desiste).
        Array.prototype.forEach.call(form.querySelectorAll('[name^="entrevistado:"]'), function (c) {
          c.checked = alvo.checked;
          c.closest('.cand').classList.toggle('feita', alvo.checked);
        });
      }
      if (alvo.name && alvo.name.indexOf('entrevistado:') === 0) {
        alvo.closest('.cand').classList.toggle('feita', alvo.checked);
        if (alvo.checked) { form.querySelector('[name="presente"]').checked = true; }
      }
      form.classList.toggle('presente', form.querySelector('[name="presente"]').checked);
      if (alvo.hasAttribute('data-auto')) { gravar(form); } else { form.classList.add('por-gravar'); }
    });
  });
})();
