# Plataforma da Feira de Emprego (GEC Carnide, FE2026)

Solução usada na XI Feira de Emprego de Carnide (13 Out 2026) para receber candidaturas a vagas de várias empresas, fazer a triagem, agendar entrevistas, acompanhar o dia do evento e avaliar. Publicada como base ou inspiração para outras organizações, por exemplo a REDEmprega Lisboa.

**Documentação completa:** [`docs/index.html`](docs/index.html) (arquitectura, ligação formulário ↔ dashboards, reabertura de vagas, stack, como reproduzir, limites).

## Como funciona, em três linhas

1. O formulário (Formbricks) só recolhe respostas. `exportar_candidaturas.py` copia-as de 5 em 5 minutos para uma base SQLite.
2. Os dashboards (`dashboard/`, Flask) escrevem nessa base: validar, anular, atribuir técnico, marcar horário, receção, seguimento, avaliação.
3. `r1_capacidade.py` corre de minuto a minuto, lê a base e as respostas e altera o formulário publicado por API: retira a empresa esgotada e **repõe-na quando uma anulação liberta uma vaga**.

## Evolução proposta

Em 2026 as empresas e as vagas foram recolhidas pelos técnicos e carregadas à mão. O passo seguinte é um formulário de inscrição das empresas (ofertas, requisitos, nº de RH, disponibilidade, duração e limite de entrevistas) que gere o formulário de candidaturas, a agenda e os perfis dos dashboards. Ver [`docs/index.html`](docs/index.html#empresas). Formulário de demonstração (sem dados reais): https://form-gec.cloudkeep.pt/s/cmwul92dx000001owwm5qm8dc

## Alternativas ao Formbricks

Testaram-se também o HeyForm (a lógica condicional não permite o limite de 3 empresas em tempo real) e o LimeSurvey (quotas nativas e gratuitas, mas pouco adequado a alterar o formulário em produção e com backoffice mais pesado). Só o exportador e o R1 dependem do Formbricks. Detalhe em [`docs/index.html`](docs/index.html#alt).

## Conteúdo

| Pasta ou ficheiro | Função |
|---|---|
| `dashboard/` | Aplicação Flask, templates, e-mails, testes |
| `exportar_candidaturas.py`, `r1_capacidade.py`, `entidades.py`, `fechar_formulario.py` | Exportação, capacidade e regras, unificação de entidades, fecho do formulário |
| `demo/criar_demo.py` | Base de demonstração com dados fictícios |
| `ct127/`, `ct129/` | Cron, serviços systemd e wrappers de cada contentor |
| `*.json` | Configuração de **exemplo** (empresas, aliases, mapeamento do formulário) |
| `docs/` | Documentação |

## Antes de usar

- Os `*.json` são exemplos. O mapeamento (`formbricks-empresas-mapping.json`) tem de ser gerado para o seu formulário.
- `dashboard/static/*.png` são imagens neutras: substitua-as pelos seus logótipos.
- Nomes, e-mails e domínios foram substituídos por valores fictícios (`exemplo.pt`, `exemplo.org`). Configure os seus por variáveis de ambiente (`FE2026_*`, ver `ct129/*.env`).
- O código está em português e com datas e regras da edição de 2026. Espere adaptar.
- Testes: `python3 -m unittest test_exportar_candidaturas test_entidades test_r1_capacidade`. Os do dashboard correm com o Flask 2.2 e o pypdf 3.4 do Debian 12 (`cd dashboard && python3 -m unittest test_dashboard`); três dependem de ambiente e podem falhar fora do contentor original.

## Dados pessoais

A solução trata NIF, documentos, CVs e contactos de candidatos. Quem a usar é responsável pelo RGPD: base legal, prazos, subcontratantes e avaliação de impacto.

## Segurança

Opções de endurecimento para uma stack como esta em [`docs/SEGURANCA.md`](docs/SEGURANCA.md).

## Licença

Código: [EUPL-1.2](LICENSE). Documentação: [CC BY-SA 4.0](docs/LICENSE-CC-BY-SA-4.0.txt). Marcas e logótipos não estão incluídos nem licenciados.

Copyright © 2026 Eduardo Ferreira. Desenvolvido para o GEC – Grupo de Empregabilidade de Carnide e cedido sem contrapartida.
