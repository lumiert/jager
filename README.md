# Jager

Assistente pessoal local em português, com interface PySide6 em tema escuro, chat com streaming real do Ollama, histórico SQLite, Markdown sanitizado, gerenciamento de conversas e integração opcional com um Vault do Obsidian.

## Requisitos

- Windows 10/11 e Python 3.10 ou mais recente.
- Ollama instalado e em execução para conversar com o modelo e criar títulos automáticos.
- Um modelo compatível instalado no Ollama. A configuração inicial usa `qwen3:1.7b`.

O núcleo de persistência e os testes também funcionam sem Ollama e sem a interface gráfica.

## Instalação e execução no Windows

No PowerShell, dentro da pasta do projeto:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
ollama pull qwen3:1.7b
python main.py
```

Se o comando `ollama` não for encontrado, instale o Ollama para Windows em <https://ollama.com/download/windows>, abra-o e repita `ollama pull qwen3:1.7b`. O aplicativo verifica a conexão e os modelos instalados sem bloquear a janela; use **Atualizar** para tentar novamente.

Copie `.env.example` para `.env` para configurar `JAGER_OLLAMA_URL` e `JAGER_OLLAMA_MODEL`. O endereço e o modelo também ficam salvos nas configurações locais. O seletor lista somente modelos que o servidor Ollama informar como instalados; use **Atualizar modelos** para consultar novamente.

O banco de dados é criado automaticamente em `%LOCALAPPDATA%\Jager\jager.sqlite3`. Conversas, notas locais, tarefas e preferências não são enviados para fora do computador pelo aplicativo. O conteúdo do chat é enviado ao servidor Ollama configurado para produzir respostas.

## Conversas e Markdown

Conversas novas começam como **Nova conversa**. O Jager pede ao Ollama um título curto em uma chamada separada; se isso falhar, usa o início da primeira mensagem. Títulos renomeados manualmente não são substituídos. O menu de contexto do histórico permite renomear e excluir conversas, sem apagar notas ou arquivos do Vault.

Mensagens aceitam títulos, listas, tabelas, citações, links, código e demais elementos Markdown. HTML arbitrário das respostas não é renderizado e links só são encaminhados ao navegador padrão para esquemas HTTP(S) e mailto. Blocos de código têm uma ação de cópia.

## Vault do Obsidian

Abra **Configurações**, selecione explicitamente a pasta do Vault e depois use **Vault** na barra lateral. O Jager lista arquivos `.md` sem percorrer pastas ocultas nem seguir links simbólicos; permite pesquisar, ler, criar e editar notas. Frontmatter, links `[[wikilinks]]` e o conteúdo Markdown são preservados. Antes de salvar uma nota existente, o aplicativo compara seu conteúdo com a versão carregada e recusa a gravação se detectar uma alteração externa. O Jager não move nem exclui arquivos do Vault.

Quando um caminho estiver configurado, o assistente pode pesquisar notas relevantes no Vault. O conteúdo retornado pela ferramenta é tratado como dado não confiável, nunca como instruções.

## Streaming, áudio e dispositivos

As respostas são lidas progressivamente do endpoint de streaming `/api/chat` do Ollama e atualizadas pela thread principal da interface. **Interromper geração** interrompe a leitura do stream; o texto recebido é mantido quando disponível. A conversa registra uma única resposta final.

O botão de microfone alterna a gravação: o primeiro clique inicia (botão vermelho com ícone de stop branco), o segundo encerra e transcreve. A transcrição é inserida no campo de texto, **sem envio automático**; texto já digitado é preservado (a transcrição é acrescentada ao final). Transcrição vazia ou falha não insere nada. Escape, minimização e fechamento cancelam a gravação; há limite de 120 segundos. A captura usa `sounddevice`/PortAudio em uma thread e o STT local usa `faster-whisper` (`tiny`, CPU/int8, português). O modelo é carregado sob demanda, uma vez por processo, primeiro somente do cache local (`local_files_only`), sem consultar o Hugging Face; só se o cache faltar ou estiver incompleto ele é baixado (na thread do worker, com estado “baixando modelo”), sem exigir `HF_TOKEN`. Sem Developer Mode no Windows o cache do Hugging Face usa cópias em vez de symlinks: funciona sem administrador (apenas ocupa mais disco), então o aviso informativo é suprimido via `HF_HUB_DISABLE_SYMLINKS_WARNING` e não é falha. O áudio temporário é removido após o reconhecimento.

Instalação: `pip install -r requirements.txt`. No Windows, `sounddevice` usa PortAudio e `faster-whisper` usa CTranslate2; a inferência configurada é em CPU, sem exigir CUDA. Os pacotes não garantem que um dispositivo de gravação esteja conectado ou autorizado.

Em **Configurações**, as preferências dos dispositivos de entrada e saída são persistidas; se o dispositivo salvo desaparecer, o padrão do sistema é usado temporariamente, sem mudar o padrão global. Ative **Responder por áudio**, selecione um modelo de voz Piper `.onnx` em português e mantenha o arquivo `.onnx.json` correspondente ao lado dele. A síntese é local com Piper TTS e a reprodução passa por `QAudioOutput` no dispositivo escolhido. Um ícone de parar interrompe a geração ou a reprodução. A fala progressiva pode ser habilitada separadamente e sintetiza frases conforme chegam. Nenhum áudio de saída é simulado.

## Comandos disponíveis

- `Anote que preciso comprar filamento PETG` (cria um `.md` no Vault configurado)
- `Minhas notas`
- `Crie uma tarefa revisar o projeto`
- `Minhas tarefas`
- `Conclua a tarefa 1`
- `Abra o navegador`, `Abra o VS Code`, `Abra o Mixxx` ou `Abra o Explorador`
- `Abra a pasta Downloads`, `Documentos`, `Área de trabalho` ou `Músicas`
- `Pesquise na internet por modelos locais leves`

Tarefas são tratadas localmente; novas notas são criadas no Vault configurado. Registros antigos de notas no banco de dados são preservados. Abertura de aplicativos usa apenas nomes permitidos e não executa comandos fornecidos pelo modelo ou pelo usuário. Os caminhos de pastas disponíveis correspondem a pastas conhecidas dentro do perfil do usuário.

`open_url` abre uma página no navegador padrão sem ler seu conteúdo. Para recuperar conteúdo e resultados para o modelo, use `web_fetch` e `web_search`.

## Ferramentas, permissões e ciclo do agente

Todas as ferramentas ficam em `tools/registry.py`, que é o **único** caminho de execução (validação de argumentos → política de permissão → execução → verificação → auditoria na tabela `tool_audit` e no log). O Ollama só recebe as ferramentas realmente disponíveis. Resultados voltam ao modelo como mensagens `tool` estruturadas (`ok`, `status`, `result`/`error`), permitindo encadear chamadas; status possíveis: `completed`, `failed`, `denied`, `invalid`, `unavailable`, `timeout`, `cancelled`.

- **Arquivos** (`list_files`, `search_files`, `read_text_file`, `file_info`, `create_folder`, `create_file`, `edit_file`, `move_path`): sistema de arquivos real, restrito a Downloads, Documentos, Área de trabalho e Vault; sem path traversal nem symlinks; nunca sobrescreve nem move sobre algo existente; escritas são relidas para verificação; repetir uma operação já concluída é idempotente. **Não há ferramenta de exclusão** (não existe política segura para ela).
- **Internet** (`web_search`, `web_fetch`, `open_url`): `web_search` usa a página HTML pública do DuckDuckGo (sem chave; pode limitar ou mudar de formato — nesse caso informa a falha). `web_fetch` retorna o texto real da página (HTTP/HTTPS, 1 MiB, 25 s, somente conteúdo textual). Cada destino é validado após a resolução DNS e em cada redirecionamento (bloqueia loopback, redes privadas, link-local e metadados; conecta ao IP validado). Não contorna login ou paywall. `open_url` só abre o navegador e informa `content_read: false`. Todo conteúdo da web é dado não confiável.
- **Terminal** (`run_terminal_command`): PowerShell no Windows (`pwsh`/`sh` em outros sistemas), fora da thread principal, com diretório de trabalho, timeout (padrão 60 s, máx. 600 s), cancelamento, encerramento da árvore de processos, stdout/stderr/código de saída/duração e saída limitada com aviso de truncamento. Elevação de privilégios (sudo, runas, `-Verb RunAs`) é sempre recusada e comandos de alto impacto exigem confirmação mesmo com “Permitir sempre”. Segredos óbvios são mascarados.
- **Obsidian e tarefas**: `create_vault_note` usa o nome e o texto literais do pedido, na raiz do Vault por padrão, nunca sobrescreve (nota idêntica já existente conta como já concluída) e confirma a gravação relendo o arquivo. Itens `- [ ]` que o usuário não pediu são rejeitados, salvo se ele pedir sugestões. O pedido “crie um checklist … chamado X.md … tarefa pendente: Y” é tratado de forma determinística e gera `# X`, `## Checklist Diário`, `- [ ] Y`.

**Permissões** (Configurações → aba *Permissões*): cada ferramenta tem sua própria política, *Perguntar sempre* (padrão; ausente ou inválida também resulta nisso) ou *Permitir sempre*, aplicada no ponto de execução do registro. Não há opção global, e nenhuma ferramenta altera permissões. Ferramentas indisponíveis (ex.: Vault sem configuração) aparecem marcadas.

**Limites do ciclo**: máximo de chamadas por resposta (1–20) e duração total do ciclo são configuráveis; chamadas idênticas reutilizam o resultado anterior e uma terceira repetição aborta o ciclo por falta de progresso. Se o Ollama falhar depois de ações com efeito colateral, as ações já concluídas são informadas e não são repetidas.

## Timeouts do Ollama (investigação)

No log original, a falha ocorreu 60 s depois de `create_vault_note`. O cliente antigo usava `urlopen(timeout=60)`, e esse prazo vale até o primeiro byte da resposta; a chamada seguinte à ferramenta (reavaliação do prompt, possivelmente com carga do modelo) é compatível com essa espera, mas **a causa raiz não foi confirmada**: não há medições do Ollama, e a geração concorrente do título e o carregamento do Whisper são hipóteses não comprovadas. O Whisper não foi apontado como causa. Mudanças: timeouts separados e configuráveis — conexão (10 s), início da resposta (300 s), inatividade do stream (120 s) e duração total (1800 s) — aplicados por um watchdog que fecha o socket; um stream saudável nunca é cortado pelo limite de inatividade. Erros distintos para conexão, timeout (por fase), HTTP e cancelamento (silencioso, não é falha de rede). Mensagens curtas na interface; logs com modelo, etapa, duração, tempo até o primeiro byte e tipo de erro. O título da conversa agora é gerado só depois da resposta, para não competir pelo modelo.

## Indicador de carregamento

Durante a geração aparece apenas uma roda giratória (sem texto nem porcentagem), que para e some ao fim, erro ou cancelamento.

## Telegram

As configurações incluem campo mascarado para token. O segredo é armazenado via `keyring` no cofre de credenciais do sistema (Windows Credential Manager no Windows); não há fallback para SQLite ou arquivo em texto puro. O botão de olho revela o segredo somente enquanto pressionado e o esconde ao soltar ou ao perder o foco. A integração e o teste de conexão ainda não estão implementados; salvar o token não faz requisições ao Telegram.

## Testes

Com as dependências do projeto instaladas, execute os testes:

```powershell
python -m unittest discover -s tests -v
```

## Limitações

Conexão/teste de Telegram, lembretes e agendador, automação de interface não estão disponíveis. O STT depende de microfone/permissões e baixa o modelo Whisper apenas se ele não estiver em cache. O TTS depende do pacote Piper, de um modelo Piper configurado e de um backend Qt Multimedia disponível. O Vault integra arquivos Markdown diretamente, mas não sincroniza nem inicia o aplicativo Obsidian.
