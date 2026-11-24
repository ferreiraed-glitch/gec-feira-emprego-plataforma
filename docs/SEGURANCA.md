# Segurança: opções para uma stack como esta

Stack tipo: Formbricks (Docker) + dashboards Flask + SQLite em contentores LXC, num Proxmox próprio, expostos por Cloudflare Tunnel. As opções estão por ordem de custo/benefício.

## 1. À frente de tudo: Cloudflare (gratuito)

| Medida | Para quê |
|---|---|
| **Cloudflare Access** (Zero Trust, até 50 utilizadores grátis) em `/auth/*`, `/admin` e nas consolas | O painel de administração deixa de estar na internet: só entra quem passa pelo login (e-mail com código, Google, etc.) |
| **Regras de limitação de pedidos** (Rate limiting) nos dashboards e nas rotas de formulário | Trava tentativas repetidas de tokens e envios em massa |
| **WAF / Bot Fight Mode** | Filtra bots e ataques conhecidos |
| **HSTS** e cabeçalhos | HSTS no domínio inteiro; o dashboard já envia CSP, `nosniff`, `X-Frame-Options` |
| **Turnstile** (CAPTCHA gratuito) no formulário público | Evita spam e envios automáticos |
| **Autenticar o túnel** | Só o túnel chega aos serviços; fechar portas no router |

## 2. Formbricks

- `SIGNUP_DISABLED=1` depois de criar as contas necessárias. Convites só por administrador.
- Painel atrás do Access (acima). Formulários públicos continuam acessíveis em `/s/<id>`.
- Chaves API: uma de **leitura** para tudo o que está exposto e uma de gestão só para o R1; validade curta e rotação depois do evento.
- Limitar extensões e tamanho de ficheiros enviados; tratar o conteúdo como não fiável.
- Fechar o formulário quando acabam as inscrições (`completed`).
- Actualizar a imagem com regularidade e ler as notas de segurança do projecto.

## 3. Dashboards (Flask + SQLite)

- Já feito: tokens de 192 bits guardados em hash, cookie HttpOnly/Secure/SameSite, CSRF em todos os POST, verificação de `Origin`, CSP restritiva, registo de acessos, âmbito por perfil.
- Validar sempre o que vem do formulário público (endereços de ficheiros, nomes, identificadores) antes de o usar num pedido ou num caminho.
- Limitar o registo de tentativas falhadas (uma linha por IP e por minuto) para não encher o disco.
- HSTS na própria aplicação.
- Aceitar `CF-Connecting-IP` só quando o pedido vem do túnel (ligar a aplicação a `127.0.0.1` ou ao IP do `cloudflared`).
- Verificar o conteúdo dos CV (estrutura PDF) e servi-los com tipo fixo; abrir ficheiros de origem desconhecida num visualizador isolado.
- `pip-audit` ou equivalente às dependências; no Debian, `unattended-upgrades` para segurança.

## 4. Contentores e rede (Proxmox/LXC/Docker)

- **Segmentação:** formulário, dados/dashboards e demonstração em contentores separados (já assim). Firewall do Proxmox: só as portas necessárias entre eles; o dashboard não precisa de chegar ao PostgreSQL.
- **MinIO e PostgreSQL:** portas em `127.0.0.1` ou na rede interna, nunca em `0.0.0.0`; consola do MinIO só por túnel autenticado ou desligada.
- **LXC não privilegiado** sempre que possível (o CT dos dados já é); privilegiado só onde o Docker o exige.
- **Serviços systemd endurecidos:** `NoNewPrivileges`, `ProtectSystem=strict`, `PrivateTmp`, `InaccessiblePaths` (já usado na demonstração), `ReadWritePaths` mínimos.
- **Utilizador de sistema próprio**, ficheiros de credenciais modo 640, BD modo 600.
- **SSH:** só chave, sem root por palavra-passe, `fail2ban` ou equivalente, acesso por VPN (WireGuard/Tailscale).

## 5. Dados e RGPD

- Backups **cifrados** e testados, e uma cópia fora do host (o PBS parado era uma lacuna).
- Eliminação no prazo previsto (apagar o contentor e os backups).
- Minimização: as empresas não vêem NIF nem documento; CV com marca de água.
- Registo de tratamento, avaliação de impacto e contratos com subcontratantes (Cloudflare, e-mail).
- Demonstrações só com dados fictícios, em BD separada que recusa escrever na de produção.

## 6. Monitorização

- Alertas de falha de cron e de serviço (R1, exportador, backup): um e-mail ou notificação basta.
- Ver os registos de acessos recusados; picos indicam tentativas.
- Uptime Kuma ou equivalente, e verificar a validade dos certificados.
- Rever as regras de Access, as chaves API e os tokens activos no fim de cada evento.

## Prioridade sugerida

1. Cloudflare Access no painel do Formbricks e `SIGNUP_DISABLED=1`.
2. Rate limiting e HSTS no dashboard.
3. Chave API de leitura para o dashboard.
4. MinIO e bases de dados só em rede interna.
5. Backups cifrados fora do host.
6. Endurecimento dos serviços e actualizações automáticas.
