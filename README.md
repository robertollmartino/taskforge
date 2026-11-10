# TaskForge — Gerenciador de Tarefas Personalizável 🚀

Aplicativo web completo, open source, leve, moderno e **Mobile-First** para gerenciamento avançado de tarefas, construído em **Python 3.12 + FastAPI + SQLite (WAL) + Tailwind CSS + PWA Web Push VAPID**.

Projetado para rodar em alta performance no **EasyPanel** via Docker, com integração nativa via REST API e Webhooks para **n8n, ChatGPT, Codex, Hermes** e agentes de IA.

---

## ✨ Funcionalidades Principais

1. **Gestão Completa de Tarefas**
   - ID único automático (UUID) + código sequencial amigável (`#101`, `#102`...).
   - Título, descrição detalhada, observações internas e cor personalizada por tarefa.
   - Data de criação automática e **data/horário de vencimento opcionais** (permite tarefas sem data).
   - Prioridades **P1 (Urgente)**, **P2 (Alta)**, **P3 (Normal)** e **P4 (Baixa)**.
   - Status padrões (**Pendente**, **Em andamento**, **Concluída**, **Cancelada**) + criação de **status personalizados ilimitados**.
   - Responsável principal, múltiplos participantes, projetos/categorias e etiquetas (tags).
   - **Subtarefas/Checklists**, **comentários**, **anexos** (upload/download) e **recorrência configurável** (diária, semanal, mensal) que gera automaticamente a próxima ocorrência ao concluir.
   - **Histórico de auditoria** completo por tarefa registrando autor, data/hora e valores alterados.

2. **Campos Personalizados Dinâmicos (Sem Alterar Código)**
   - Tipos suportados: `short_text` (Texto curto), `long_text` (Texto longo), `number` (Número), `currency` (Moeda R$), `date` (Data), `datetime` (Data/Hora), `checkbox`, `select` (Seleção única), `multi_select` (Múltipla seleção) e `url`.
   - Suporte a obrigatoriedade, valor padrão, exibição em colunas, ordenação, filtros avançados e API REST.

3. **Visualizações Personalizadas & Motor de Filtros Multi-Projetos**
   - 3 modos de visualização principais: **Tabela** (com edição rápida in-line e adaptação mobile em cards), **Kanban** (colunas com drag-and-drop) e **Lista**.
   - Presets prontos: *Todas as Tarefas, Quadro Kanban Geral, Hoje, Atrasadas, Próximos 7 dias, Sem data, Prioridade P1, Por responsável, Concluídas*.
   - Criação ilimitada de visualizações salvas (criar, editar, duplicar, excluir, definir como padrão, manter privada ou compartilhar).
   - **Construtor de Filtros Avançados:** combina condições usando lógica **E (`AND`)**, **OU (`OR`)** e **subgrupos aninhados**, consultando simultaneamente **múltiplos projetos** tanto em campos nativos quanto personalizados.

4. **PWA Instalável no Android & Notificações Web Push (VAPID)**
   - Manifesto PWA (`manifest.json`), ícone vetorial gradiente e Service Worker (`sw.js`) com suporte a Web Push em segundo plano no Android.
   - Geração automática e persistência de chaves criptográficas **VAPID (P-256)** em `/app/data/vapid.json`.
   - Lembretes configuráveis por tarefa e resumos automáticos de tarefas pendentes às **08:00, 13:00 e 18:00** (configuráveis por usuário) no fuso horário `America/Sao_Paulo`.
   - Distinção automática entre tarefas **Atrasadas**, **Para Hoje** e **Futuras**, com chave de desduplicação (`dedup_key`) para evitar notificações repetidas.

5. **Multiusuário, Papéis e Permissões Granulares (RBAC)**
   - Papéis: `admin` (total), `supervisor` (visualiza e gerencia tarefas de toda a equipe), `member` (funcionário — visualiza apenas suas tarefas atribuídas/participantes) e `viewer` (somente leitura).
   - Permissões sobrescritas por projeto e por usuário, validadas rigorosamente no backend e na API.

6. **API REST v1 Documentada (Swagger/OpenAPI) & Observabilidade**
   - Documentação interativa em `/docs` e especificação em `/api/v1/openapi.json`.
   - Autenticação via JWT Bearer ou **Tokens de API (`X-API-Key: tf_live_...`)** com escopos.
   - **Executions Log (`executions.db`):** middleware em `core/audit.py` que grava todas as chamadas de API/Webhooks com duração em ms e disponibiliza consulta em `GET /executions` (JSON ou dashboard HTML em `/executions?format=html`).

7. **Webhooks Configuráveis com Assinatura HMAC & Retentativas**
   - Disparo automático em eventos (`task.created`, `task.updated`, `task.deleted`, `task.completed`, `task.reopened`, `task.assigned`, `task.comment_created`, `task.due_date_changed`).
   - Assinatura `X-TaskForge-Signature: sha256=...` (HMAC-SHA256), filtro por projetos, retentativas automáticas com backoff exponencial e histórico completo de entregas.

8. **Backup Consistente Online do SQLite + Anexos**
   - Snapshot quente usando a API nativa `sqlite3.Connection.backup()` empacotado em `.zip` junto com a pasta de anexos, sem necessidade de desligar a aplicação.
   - Agendamento automático via cron, política de retenção configurável e restauração online via API ou painel.

---

## 🛠️ Variáveis de Ambiente (`.env`)

| Variável | Padrão | Descrição |
|---|---|---|
| `APP_NAME` | `TaskForge` | Nome da aplicação |
| `APP_ENV` | `production` | Ambiente (`development` ou `production`) |
| `PORT` | `8000` | Porta HTTP interna do container |
| `TZ` | `America/Sao_Paulo` | Fuso horário padrão para lembretes e resumos |
| `SECRET_KEY` | *(obrigatório alterar)* | Chave secreta para assinatura de tokens JWT |
| `DEFAULT_ADMIN_USER` | `admin` | Usuário administrador criado no primeiro boot |
| `DEFAULT_ADMIN_EMAIL` | `admin@taskforge.local` | E-mail do administrador inicial |
| `DEFAULT_ADMIN_PASSWORD` | `Admin@123456` | Senha inicial do administrador |
| `WEBHOOK_RECEIVER_TOKEN` | `wh_sec_token_change_me` | Token para webhook de entrada (`POST /webhooks/inbound`) |
| `DATA_DIR` | `/app/data` | Diretório persistente (volume Docker) |
| `BACKUP_RETENTION_COUNT` | `10` | Quantidade máxima de backups mantidos automaticamente |
| `VAPID_PUBLIC_KEY` | *(auto-gerado)* | Chave pública VAPID (gerada em `/app/data/vapid.json` se vazia) |
| `VAPID_PRIVATE_KEY` | *(auto-gerado)* | Chave privada VAPID |
| `VAPID_CLAIM_EMAIL` | `mailto:admin@taskforge.local` | Contato técnico para o serviço Web Push |

---

## 🐳 Instalação Local e no EasyPanel (Docker)

### 1. Execução via Docker Compose
```bash
cp .env.example .env
docker compose up -d --build
```
Acesse `http://localhost:8000` (Documentação da API em `http://localhost:8000/docs`).

### 2. Instalação no EasyPanel (VPS com Proxy HTTPS)
1. Crie um novo serviço do tipo **App** no seu projeto do EasyPanel.
2. Em **Source**, conecte ao seu repositório GitHub (branch `main`) e selecione **Dockerfile** como método de build.
3. Em **Environment**, cole as variáveis do arquivo `.env.example` (defina um `SECRET_KEY` e `DEFAULT_ADMIN_PASSWORD` seguros).
4. Em **Mounts / Volumes**, adicione um volume persistente apontando para:
   - **Mount Path:** `/app/data`
   *(Isso garante persistência total do banco `tasks.db`, `executions.db`, chaves `vapid.json`, `attachments/` e `backups/`).*
5. Em **Domains**, configure seu domínio HTTPS apontando para a porta interna `8000`. O Uvicorn já está configurado com `--proxy-headers --forwarded-allow-ips=*` para funcionar perfeitamente atrás do proxy HTTPS do EasyPanel (requisito essencial também para o Service Worker e Web Push no Android).

---

## 💾 Backup, Atualização e Recuperação

- **Criar Backup Manual via Interface ou API:**
  - Pelo Painel: abra **Configurações → Backups SQLite → Criar Backup Agora** e clique em **Baixar .zip**.
  - Pela API: `POST /api/v1/backups` com header `Authorization: Bearer <TOKEN>` ou `X-API-Key: <API_TOKEN>`.
- **Restauração Online:**
  - Pelo Painel: clique em **Restaurar** ao lado de qualquer backup listado, ou chame `POST /api/v1/backups/{backup_id}/restore`. O banco SQLite e os anexos são restaurados a quente com verificação prévia de `PRAGMA integrity_check`.
- **Atualização da Aplicação no EasyPanel:**
  - Como todos os dados residem no volume `/app/data`, basta fazer push para o GitHub ou clicar em **Deploy** no EasyPanel. O novo container subirá preservando integralmente o banco, anexos e backups.

---

## 🧪 Executando os Testes Automatizados

```bash
pip install -r requirements.txt
pytest -v
```
