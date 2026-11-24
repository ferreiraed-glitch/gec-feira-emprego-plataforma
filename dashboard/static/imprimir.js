// Botão "Imprimir": a política de segurança das páginas não deixa pôr o onclick no HTML.
document.querySelectorAll("[data-imprimir]").forEach(function (b) {
  b.addEventListener("click", function () { window.print(); });
});
